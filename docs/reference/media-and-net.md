# Media understanding and outbound connectivity

Reference material moved out of `AgentMD.md` (§53 there lists the `must` / `never` rules from these sections in one place).

The text below is verbatim; it was not edited during the move.

## Contents

- [18. Outbound AI connectivity: which IP family](#s18)
- [22. Media understanding](#s22)

---

<a id="s18"></a>

## 18. Outbound AI connectivity: which IP family

The AI calls leave this host over **IPv6 first, IPv4 as a fallback**. That is
not an accident of the kernel's address selection — `app/net.py` makes it an
explicit, reported, reversible decision. The requirement behind it was
unreliable IPv4 connectivity from this server to the AI APIs.

### 18.1 What the module does, and what it refuses to do

Three steps, and it deliberately stops there:

1. **Report.** `describe()` says whether IPv6 is actually usable (a *global*
   address, not the `fe80::` every interface has), what each family costs to
   reach, and which family a connector will try first. Logged once at startup as
   `AI egress: ipv6_usable=… order=… prefer=… global_v6=…`, so "the AI calls are
   flaky" can be diagnosed as an address-family problem instead of guessed at.
2. **Prefer.** `install_preference()` orders resolved addresses IPv6-first for
   the AI hosts. This is a **reorder, never a filter**: every IPv4 address stays
   in the list, so a connector that walks it gets IPv6 when it works and IPv4
   when it does not. That is the safe fallback, and it is why being wrong here
   costs a slower call rather than a call that cannot be made.
3. **Scope.** `getaddrinfo` has no per-call hook, so the wrapper is
   process-wide — and is therefore restricted to a hard-coded set of AI host
   names, returning everything else untouched. An explicit family request
   (`AF_INET`) is passed straight through, unsorted, because a caller that asked
   has already decided.

It does **not** bind a source address, disable IPv4, or add a third-party
resolver. Each of those would turn a preference into a dependency.

`install_preference()` is called from `main()` *before* anything opens a socket,
and it declines rather than guesses: with `AI_PREFER_IPV6=0`, or on a host with
no global IPv6 address, it does nothing and says which. The failure mode of
being wrong is a slower call.

### 18.2 Verifying it

```bash
# The startup line: is the preference installed, and what order will be used?
docker compose logs | grep "AI egress"

# A live probe: both families, in milliseconds. null means that family failed.
docker compose exec -T guardbot python -c \
  "import sys; sys.path.insert(0,'/srv'); from app import net; \
   print(net.describe()); print(net.probe('generativelanguage.googleapis.com'))"
```

Measured on this host 2026-09-21: `ipv6_usable=True`, `order=['IPv6','IPv4']`,
`prefer=installed`, and both families connect (`IPv6` 6.3 ms, `IPv4` 5.2 ms).
The host holds one global address, `2a14:7c0:1742:3be0::/64`, and the container
runs `network_mode: host`, so the container sees it too.

---

<a id="s22"></a>

## 22. Media understanding

`app/media.py`. One builder, one caller: the assistant. The moderation path
that used to share it was removed, so what remains is the assistant's
translation of a file the user explicitly sent it into something Gemini can
read. Nothing here feeds any content moderation.

### 22.1 What was measured, and what it decided

One real call per row on this deployment's key, 2026-09-21:

| MIME | Transport | Result |
|---|---|---|
| image/png | inline | described correctly |
| image/gif | inline | described correctly |
| video/mp4 | inline | described correctly |
| video/webm | inline | described correctly |
| audio/wav | inline | described correctly |
| audio/ogg | inline | accepted |

The Files API also works (upload → `PROCESSING` → `generateContent` by URI →
delete) and is **deliberately not used**: it would leave a copy of a group
member's media in Google's storage for the life of the file, for no capability
this bot needs. Telegram's own download ceiling is 20 MB and the inline request
ceiling is the same order, so there is nothing the Files API would unlock here.

### 22.2 Every Telegram media type

| Telegram | kind | how it is analysed |
|---|---|---|
| photo | `photo` | the largest size, inline as an image |
| static sticker | `sticker` | WebP, converted to PNG with Pillow |
| animated sticker (`.tgs`) | `animated_sticker` | the still preview Telegram attaches — Lottie is not readable by ffmpeg or the model, and the kind says so |
| video sticker (`.webm`) | `video_sticker` | inline as video |
| GIF / animation | `gif` | inline as video (Telegram sends MP4) |
| video | `video` | inline as video |
| round video note | `video_note` | inline as video |
| image document | `image_file` | inline as an image |
| video document | `video_file` | inline as video |
| voice note | `voice` | transcription (§24) |
| audio file | `audio` | transcription (§24) |
| anything else | — | `describe()` returns None: not analysed, and it says so |

### 22.3 The fallbacks, and why each exists

* **Oversized file** → the thumbnail, if Telegram attached one, with
  `thumbnail_only` set so the caller can say the read was on a preview.
* **Long video** (`GEMINI_MEDIA_MAX_SECONDS`) → `GEMINI_MEDIA_FRAMES` still
  frames, sent as images. This is the documented API approach, and it is why a
  long clip does not silently become "not analysed".
* **Long audio** → **refused**, not truncated. Half a sentence is a wrong
  sentence, and this module will not pretend otherwise.
* **A container the API will not take** → for a video, one more attempt as
  frames; for anything else, refused.
* **A download that fails** → `ok=False` with a reason. Nothing fabricates a
  description.
