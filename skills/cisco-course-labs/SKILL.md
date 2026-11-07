---
name: cisco-course-labs
description: Execute and extend CiscoAgent coursework scenarios for ARP, switch console and VLANs, STP and EtherChannel, RIP and OSPF, including staged failure experiments and observed reports. Use with the supported course PDFs or an explicit request for these numbered laboratory scenarios.
---

# Cisco coursework scenarios

Use the CiscoAgent application executor, not a checklist handed back to the student. The reusable implementation is in `ptagent/course_runner.py`, `course_switching.py` and `course_routing.py` of the CiscoAgent repository. Locate the existing checkout before using a hard-coded path. The app recognizes the five original PDFs without a paid model, including the ARP PDF with broken text mapping.

## Inputs that change the result

- Lab 2 continues a real lab 1 `.pkt`. Read and validate every PC address, initial ARP cache, cold/warm traces and all-pairs connectivity. Distinguish ARP requests and replies by actual decision logs, not packet color.
- Lab 3 takes variant 1–20 and an ASCII IOS hostname derived from the student's name/group. The variant table fixes topology, host counts and which two VLAN groups may route. VLAN group 1 in that table is not permission to put workstations in reserved VLAN1: use groups 10/20/30 and record the mapping. Test denied inter-VLAN paths as well as successful ones.
- Lab 4 continues lab 3. Show the actual alternate path after a forwarding port fails; restore it. A correctly configured EtherChannel does not prove simultaneous traffic uses two members. Factory source MACs may hash to the same member; use observed traces to resolve the experiment and record any MAC changes.
- Labs 6 and 7 require the topology from lab 5. If it is absent, do not call a synthetic fixture the student's completed lab. The disposable fixture in `examples/create-routing-fixture.py` tests the executor only. RIP must lose its static routes, gain a cycle with a measured shorter route and recover after a router outage. OSPF must use serial links, add three routers with PCs, verify partition behavior, then redundancy and a measured next-hop change after bandwidth tuning.
- Lab 5 is recognized but requires an explicit `LabPlan` JSON matching the actual variant diagram (`--topology-plan`). The supplied PDF contains external diagram links, not the diagrams themselves. If a link says the owner has not shared it, record the missing source and never substitute the routing fixture. `course_static.py` validates one PC per router, unique /24 LAN/WAN, then measures failed inter-network pings before static routes, configures shortest next-hop routes including return/WAN paths and verifies readback, all directed pings and traceroutes to three different networks. This new scenario has unit coverage; do not describe it as live verified until a real run succeeds.

## Execution and evidence

Only attach to an already open Cisco with its MCP module running. Never auto-launch Cisco or overwrite the student's original file. The course runner saves a native backup first, uses an independent report entry for every requirement and restores temporary outages on stop/error.

Developer entry point from the checkout: `python -m ptagent.course_cli --lab NUMBER --variant NUMBER --previous ABSOLUTE_PKT_PATH --output NEW_OUTPUT_DIRECTORY`. Lab 3 creates its own networks. Omitting `--previous` uses the already open network for the other labs. The GUI opens the same executor and parameter dialog when a supported PDF is loaded.

Read `report.json`, `observations.json` and the real `.pkt` after a live run. A mock `.pkt` in a unit test is not evidence of Cisco compatibility. Failed, truncated, missing or unsupported observations remain failed/unverified in the Word and HTML reports. A ping transport timeout is not proof of a negative connectivity test. Packet loss inside a successfully returned experiment transcript is an observation rather than a bridge error.

The physical Windows ARP section is separate and opt-in. It temporarily changes a real gateway neighbor entry and needs administrator privileges. The GUI requires explicit selection; never run it merely because a PDF mentions the command. Preserve existing permanent neighbor state and verify restoration; the helper uses an independent restoration watchdog. Never commit reports, student profiles, generated lab passwords or gateway details to GitHub.
