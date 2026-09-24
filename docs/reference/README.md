# Reference index — the material split out of `AgentMD.md`

Reference material moved out of `AgentMD.md` lives in this directory, grouped by
subsystem. The `must` / `never` rules from these sections are collected in one
place in **`AgentMD.md` §53 Invariants**, which is authoritative where it and a
reference file disagree. The text in these files is verbatim; it was not edited
during the move. The section numbers point at the explanation, not at a rule's
authority.

> **Reminder — `AgentMD.md` §55 "Context Preservation & Session Handoff".**
> Before you continue work on a resumed session, read that section. It is the
> permanent, non-bypassable rule that **Git and `AgentMD.md` are the source of
> truth for continuing the work — not a session's transient chat memory**.
> Checkpoint → commit → push → verify before context loss; read → verify →
> reconcile → continue after a new session; never guess → restart → duplicate →
> reimplement. A reference file may be stale; the code and its tests are the
> authority (§0).

## The files

| file | subsystem | sections |
|---|---|---|
| [`acquisition.md`](acquisition.md) | Acquisition: the VPN bot handover | [13](acquisition.md#s13) |
| [`admin-and-audit.md`](admin-and-audit.md) | Administration, the audit trail, identity and agent data | [25](admin-and-audit.md#s25) · [29](admin-and-audit.md#s29) · [30](admin-and-audit.md#s30) · [31](admin-and-audit.md#s31) · [43](admin-and-audit.md#s43) · [44](admin-and-audit.md#s44) · [45](admin-and-audit.md#s45) |
| [`assistant.md`](assistant.md) | The conversational assistant | [17](assistant.md#s17) · [23](assistant.md#s23) · [24](assistant.md#s24) · [38](assistant.md#s38) · [40](assistant.md#s40) |
| [`coding-agent.md`](coding-agent.md) | The coding-agent bridge | [39](coding-agent.md#s39) |
| [`gemini-pool.md`](gemini-pool.md) | The Gemini account pool | [28](gemini-pool.md#s28) |
| [`integrations.md`](integrations.md) | Integrations: the capability registry, the VPN surface, the key control plane | [46](integrations.md#s46) · [49](integrations.md#s49) · [50](integrations.md#s50) |
| [`media-and-net.md`](media-and-net.md) | Media understanding and outbound connectivity | [18](media-and-net.md#s18) · [22](media-and-net.md#s22) |
| [`moderation.md`](moderation.md) | Moderation: the workloads, the policy, the filter and the ladder | [19](moderation.md#s19) · [20](moderation.md#s20) · [21](moderation.md#s21) · [32](moderation.md#s32) · [33](moderation.md#s33) · [47](moderation.md#s47) |
| [`nexus-awareness.md`](nexus-awareness.md) | Nexus and group awareness | [34](nexus-awareness.md#s34) · [35](nexus-awareness.md#s35) · [36](nexus-awareness.md#s36) · [41](nexus-awareness.md#s41) · [42](nexus-awareness.md#s42) · [43](nexus-awareness.md#s43) · [44](nexus-awareness.md#s44) · [45](nexus-awareness.md#s45) · [46](nexus-awareness.md#s46) · [47](nexus-awareness.md#s47) · [48](nexus-awareness.md#s48) · [49](nexus-awareness.md#s49) · [50](nexus-awareness.md#s50) · [51](nexus-awareness.md#s51) · [52](nexus-awareness.md#s52) · [53](nexus-awareness.md#s53) · [54](nexus-awareness.md#s54) · [55](nexus-awareness.md#s55) · [56](nexus-awareness.md#s56) · [57](nexus-awareness.md#s57) · [58](nexus-awareness.md#s58) · [59](nexus-awareness.md#s59) · [60](nexus-awareness.md#s60) · [61](nexus-awareness.md#s61) · [62](nexus-awareness.md#s62) · [63](nexus-awareness.md#s63) · [64](nexus-awareness.md#s64) |
| [`voice-live.md`](voice-live.md) | Nexus Voice Live | [51](voice-live.md#s51) |
| [`web-search.md`](web-search.md) | Web search | [52](web-search.md#s52) |

## Where the rules and the handoff rule live

- **`AgentMD.md` §53 Invariants** — every `must` / `never` / `always` rule from
  these sections, collected in one place. Authoritative over the reference
  files.
- **`AgentMD.md` §55 Context Preservation & Session Handoff** — the permanent
  rule for keeping the project continuable across sessions.
- **`AgentMD.md` §0 Authority and stale state** — the order of authority:
  actual repository state > Git history > current documentation > old handoff
  assumptions.
