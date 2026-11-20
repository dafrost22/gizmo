# How Gizmo works

## Two agents share a turn

`main.py` runs one session at a time. In a session, two Strands agents work together:

1. **The voice** (`agent.py`): a `BidiAgent` on Amazon Nova Sonic. It hears and speaks, and decides nothing. Its only tool is `think(heard)`.
2. **The brain** (`brain.py`): an `Agent` that decides what to say and do. It has the tools of the personality in use: the MCP tools, the skills, and the robot's camera.

Nova Sonic calls `think` with what it heard, the brain answers with the exact words, and Nova Sonic says them. A personality with a voice of its own (the voice add-in) is spoken by a voice server instead, and Nova Sonic stays quiet for that turn.

The session is rebuilt whenever something it depends on changes: the personality, its prompt, voice or tools, an MCP server, or the model (`config_rev`). A rebuild takes about two seconds.

| What's set | Who hears and speaks | What thinks |
|---|---|---|
| The default | Nova Sonic | The brain on the robot, with Claude Haiku on Bedrock (Sonnet stands by) |
| What thinks → OpenAI-compatible | Nova Sonic | The brain on the robot, with your endpoint's model (Claude on Bedrock stands by) |
| What thinks → Bedrock AgentCore | Nova Sonic | `AgentCoreBrain` → the hosted runtime (the brain on the robot stands by) |
| Who hears and speaks → Home box (add-in) | `LocalSession`: mic → the box's ears → the brain → a voice server, sentence by sentence | Whichever of the above is set |

**Fallbacks:**
- **A slow or failed model:** if a model doesn't speak within its budget (6 s of model time, paused while tools run), the next model in line takes the same turn from the same conversation. Every model continues from one shared history.
- **The home box is down:** Nova Sonic hears and speaks, and the app checks every 30 s for the box to come back.
- **AgentCore isn't set up:** the brain on the robot thinks, and the console says why.
- **The network changes underneath:** a travel router swapping its uplink, for instance. The stream to Nova Sonic stays open but dead, and nothing errors for minutes. The one sign is that the agent stops taking audio from the mic, because a send never completes. After 8 s of that the app closes the session, waits until a new connection can reach AWS, reconnects the MCP servers and opens a new session.

## A turn's prompt

The prompt has two layers:
1. **The personality's system prompt.** Who it is and what it's for. Each personality has its own; nothing is shared.
2. **"How you work."** Fixed mechanics the console can't break:
   - Write for the ear.
   - Gesture tags lead the reply (`[emotion:x] [look:y]`).
   - Use MCP tools rather than memory, and load a skill before answering what it covers.
   - If the personality has sound clips: their names, and the `[sound:<name>]` tag that plays one.

Then the skills are listed, by name and description only:
- **Skills on the robot:** added by Strands' `AgentSkills` plugin, loaded with its `skills` tool.
- **Skills served over MCP:** added by `McpSkills` (in `mcp_skills.py`), each with its server and URI, loaded with `read_skill(server, uri)`.

The brain writes gestures and words in one reply. `ReplyStream` turns the streamed text into gestures (each message's leading tags, as soon as each is complete) and sentences. A whole-reply voice like Nova Sonic only gets the last message, so "let me check…" before a tool call isn't spoken.

**Sound clips** (`sounds.py`) ride the same way. A personality can have short recordings, and the model plays one by writing `[sound:<name>]` at the start of a reply. The brain only reports the tag. Whoever speaks the reply puts the recording on the speaker first, and the words follow it in the same queue, so it works with Nova Sonic's voice, a voice server's, or the home box's. Clips are WAV files, decoded with Python's own `wave`, so the robot needs no audio library.

## Personalities and the toolbox

- **`personalities.py`:** a personality's name, system prompt, voice and tools, and its introduction (what it's told when asked to introduce itself).
  - **Tools:** per MCP server, whether it's on, which of its tools and which of the skills it serves (`null` means all, including ones it adds later); plus the names of skills kept on the robot.
  - **Voice:** a Nova Sonic voice by default. Each built-in personality ships with one, and a reset goes back to it. With the voice add-in it can be a tuned built-in voice or a clone of a short clip.
  - **Built-ins:** Gizmo and Young explorer keep their defaults in code. The robot only stores what the console changed.
- **`toolbox.py`:** connects each MCP server once through Strands' `MCPClient`, and hands a brain just what its personality switched on (`Selection`).
  - **Tool names:** a server's tools reach the model prefixed with its name (`explorer_flag`).
  - **The trace:** `describe()` turns each finished tool call into a line and a detail record (server, tool, arguments, a preview of the result, the time). The console's trace is built from these.
- **`demo_server/`:** an MCP server (`MCPServer`, stateless streamable HTTP) that the app runs on 127.0.0.1:8772 so there's something to try. The folder imports nothing from the rest of the app.
  - **Twelve tools:** the weather, the time anywhere, unit and currency conversion, a calculator, an encyclopedia, flags, and six for live space data.
  - **Five skills,** each a different shape: two of facts plus which tool to use (`universe`, `world-countries`), a procedure that runs three tools (`trip-briefing`), one whose content is in separate files read on demand (`quiz-night`), and one that explains the robot itself (`about-this-robot`).
  - **Times** are said in home's time zone. A robot's clock is often left on UTC, so then the zone comes from home's coordinates.

## Skills over MCP

[SEP-2640](https://modelcontextprotocol.io/seps/2640-skills-extension) lets a server serve Agent Skills as resources. The SEP is frozen; the working group maintains the extension's [specification page](https://skills.extensions.modelcontextprotocol.io/specification/beta/skills), which is what the code follows. Both sides are in this repo.

**The server's side** (`demo_server/skills_ext.py`): `serve_skills(mcp, folder)` registers every file under `<folder>/<name>/` as a `skill://<name>/...` resource, declares the `io.modelcontextprotocol/skills` extension, and answers `skills/list` and `skills/get`. Files are read at request time, so editing a `SKILL.md` while the server runs is enough.

**The host's side** (`mcp_skills.py`: `SkillsHost` for the protocol and the checks, `McpSkills` as the Strands plugin, `SkillPick` for what may load. It imports nothing else of Gizmo's, so the AgentCore runtime uses a copy of the same file):

1. **Listing.** When a server connects, `SkillsHost.list()` calls `skills/list`. An entry is dropped (and the reason kept for the console) if its `resources` is missing, its name doesn't match its URI, or it's over the SEP's limits of 512 files or 16 MiB. No file is fetched.
2. **Loading.** When the model calls `read_skill`, the `SKILL.md` is fetched with `resources/read` and checked: the byte length against the listed size, the SHA-256 against the listed digest, and the parsed frontmatter against the listing's, field by field. On a mismatch the host calls `skills/get` once (the skill may have been edited since the listing) and tries again, then refuses.
3. **Into the model's context.** The body goes in tagged with the server it came from. The frontmatter doesn't, so `allowed-tools` never reaches the model.
4. **Other files.** `read_resource` only reads files of a skill that's already loaded, from the server that served it, each checked against the entry.
5. **Skill first, then tools.** A step that loads a skill only loads skills. If the model asks for a skill and other tools in one step, `McpSkills.skill_first` (a Strands `BeforeToolCallEvent` hook) cancels the other calls with a note to read the skill and call them again, so the skill is in the conversation before any tool it describes runs. The trace shows only the calls that ran; the held ones are in the app's log.

Requests for `skills/list` and `skills/get` go over the session Strands' `MCPClient` already holds. Strands has no public way to send a custom request yet, so that one step uses two private attributes, isolated in one function.

5. **Approval.** A personality's ticks decide which skills are offered to the model.
   - **All of a server's skills ticked** means all of them, including ones it adds later. That is trust in the server, and nothing is approved one by one.
   - **Only some ticked** approves each of those as it is now: the tick keeps a fingerprint of the skill's `resources` (every file's URI and digest). If the server later lists the skill with a different set, or a load finds it changed, the skill isn't offered and can't be loaded, and the console shows "not loading" with a button to approve it as it is now. Saving anything else never renews an approval.
   - **A skill generated on request** has no digests, so it can't be approved on its own. It only loads when all of its server's skills are on.

Not implemented: `resources/directory/read`, nested skills as their own entries, a disk cache, an approval prompt in the conversation, and approving an `allowed-tools` grant (the field is always ignored).

## The console

`api.py` holds every route, apart from `main.py`, so the console can be worked on without a robot. `static/` is plain HTML, CSS and JavaScript with no build step.

- **Talk:** the conversation as turns, and the trace of the selected turn.
  - **The reply being spoken lights up word by word.** The speaker keeps each sentence's place in the reply's audio (`robot_io.Captions`): Nova Sonic sends a sentence's words just before its audio, and a voice server is asked for one sentence at a time. The console spreads a sentence's time over its words by their length.
  - **New conversation** clears the turns and rebuilds the session, so neither the brain nor the voice has a conversation behind it.
- **Personalities:** prompt, voice, tools.
- **Servers:** the inspector. It shows each server's tools and skills, runs a tool by hand, and fetches and checks a skill exactly as a load would.
- **Settings:** the model, listening, the robot, add-ins, health.
- **The header:** the personality in use, the mic, Hide and Reveal, and Shut down.

**Hide and Reveal** are for unveiling Gizmo in front of people. Hide puts it in its sleep pose with the motors off and the mic muted, and closes the cloud session; it survives restarts. Reveal wakes it, and can have it introduce itself. **Introduce yourself** does the same on request: the session loop sends the personality's introduction to the brain as a stage direction, and the reply is spoken in character.

`desk.py` serves the same console and runs the same brain on a laptop, with typed turns and stand-ins for the mic, speaker and body. It doesn't import the robot's SDK.

The page reads `?theme=light|dark`, which Reachy Mini Control passes to an app it embeds, and works at that panel's width. It asks for confirmation in its own dialog, because embedded browsers drop `confirm()` silently. It also watches its own files' ETags and offers a reload when they change, because an embedded panel has no reload button.

## Listening

`listening.py` decides which heard turns reach the brain.
- **Free talk:** everything heard is a turn.
- **Wake word:** a turn needs the name, matched fuzzily.
  - **Follow-ups:** for 20 s after Gizmo stops talking, a turn doesn't need it.
  - **Interrupting:** while Gizmo talks, only its name interrupts it.
  - **Typed lines:** always go through.
- **Reactions:** "oh", "yeah" and the like are ignored, unless Gizmo has just asked a question.
- **What it overhears:** a turn that wasn't for Gizmo is counted, and its words are neither shown on the console nor written to the log, unless "Show the words it heard but ignored" is on.

## Looking at whoever speaks

`direction.py` turns Gizmo toward a voice. The daemon reports a direction of arrival from the mic array in the head (`/api/state/doa`), and a tracker keeps the last eight seconds of readings.

- **When:** as soon as a turn is known to be for Gizmo, before the brain answers. By then the words are a second or two old, so the bearing is the median of the recent readings the mic chip flagged as speech. A short phrase may only have a couple, so readings taken while the mic was voice-loud are the second choice. Readings taken while Gizmo itself speaks don't count.
- **How far:** the head alone for a small turn, the body following past 40 degrees, within the SDK's limits. Offsets under 8 degrees aren't worth a twitch.
- **Afterwards:** the brain's `[look:left]` and the like are relative to the person, and Gizmo turns back to them after a recorded emotion, which always ends facing the room.
- **Limits:** the array tells left from right, not front from back, and is good to 10 or 15 degrees. Which side is left differs between robots, so the console has a switch that flips it.

## The AgentCore brain

[agentcore/app.py](../agentcore/app.py) is a `BedrockAgentCoreApp` whose entrypoint is an async generator, so the reply streams back as server-sent events.
- **One agent per session:** it keeps one Strands agent per `runtimeSessionId`, rebuilt when the prompt, model, skills or servers change.
- **A skills host of its own.** The runtime has its own connections to the MCP servers and runs the same `mcp_skills.py` as the robot: it lists the skills they serve, and one is fetched, checked and labelled with its server when the model loads it there.
  - The robot sends the personality's picks with each turn: per server, which tools, which skills, and the approvals of skills ticked one by one. The runtime applies them to what its own servers list, so an approval only holds there if the files are the same.
  - Skills kept on the robot aren't served by anything, so those go as text and are ordinary Agent Skills in the runtime.
- **Warm-up:** the robot uses one session per brain and warms it up when the session starts, because a new session boots a microVM in about 6 s. Warm turns take 2–3 s with tools.
- **Deploying:** [agentcore/deploy.py](../agentcore/deploy.py) builds linux/arm64 wheels, uploads a code zip to S3, creates an execution role (optionally with a permissions boundary), and creates or updates the runtime. The same script reads its logs, invokes it and deletes it.

## Lessons that shaped the code

- **Claude 5.5 on Bedrock thinks before it answers by default.** That added about 3.5 s and hundreds of tokens a call, which blew both the voice budget and the 500-token cap.
  - It's turned off per model, and each model spells it differently: Haiku takes `{"type": "disabled"}`, and Sonnet only accepts `{"type": "between_tools"}`.
- **Nova Sonic only replies while audio keeps flowing.** The mic sends silence when muted, rather than nothing.
- **A local model's first word matters more than its last.** Streaming sentences into the voice saved about half a second a turn.
- **Every tool's schema is paid for on every turn.** Picking tools per personality keeps a small model's prompt small.
- **Recorded moves plus loud speech can brown out the robot.** Moves run one at a time with a rest after each, reactions don't trigger gestures, and the app re-torques motors after a "Motor communication error".
- **Read SSE from AgentCore in small chunks.** `iter_lines()`'s default 1 KB reads hold the first words back until nearly the end of the reply.
- **An embedded browser panel answers `confirm()` with "cancel" and shows nothing.** The console's Shut down did nothing inside Reachy Mini Control until it asked in its own dialog.
