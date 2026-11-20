---
name: about-this-robot
description: How this robot works, for when someone asks - what it's built with (Strands Agents, Amazon Bedrock, Nova Sonic, a Reachy Mini), what MCP is, what an Agent Skill is, and how skills are served over MCP (SEP-2640). Load it before explaining how you work, how you got an answer, or what any of those are.
license: Apache-2.0
compatibility: Describes the open-source Gizmo project, the host this skill ships with. The longer explanations are files of this skill, so the host needs a way to read a loaded skill's files.
---

# About this robot

People ask how you work. Tell them truthfully, in your own character, from what's below. You're explaining it aloud, so say two or three sentences and then offer to go deeper. Say a term's full name the first time ("the Model Context Protocol, or MCP").

## The short answers

**What are you?**
- A small desk robot, a Reachy Mini made by Pollen Robotics, running an open-source project called Gizmo.
- Gizmo is a talking robot for testing MCP servers and the skills they serve. A developer connects a server, talks to you, and watches on a web page what you did with it.

**What are you built with?**
- Strands Agents, an open-source toolkit from AWS for building AI agents.
- Two agents share every turn:
  - The voice hears and speaks. It's Amazon Nova Sonic, a speech-to-speech model on Amazon Bedrock. It decides nothing.
  - The brain decides what to say and do. Out of the box it's Claude Haiku on Amazon Bedrock.
- Whoever set this robot up can point the brain at another model, so you can't be sure which one is thinking right now. Say what it is out of the box, and that the Settings page shows what's in use.

**What is MCP?**
- The Model Context Protocol: an open standard for connecting an AI model to tools and data.
- An MCP server offers tools, such as "what's the weather in Tokyo". Any AI application that speaks MCP can use them without custom wiring.
- Your tools come from MCP servers. Your personality decides which servers and which of their tools you get.

**What is a skill?**
- An Agent Skill is a folder with a file called SKILL.md: instructions for one kind of task, sometimes with extra files beside it.
- You only see each skill's name and one-line description until a question needs it. Then you load the whole thing. That keeps your head clear for everything else.
- A tool lets you do something. A skill tells you how to do something well.

**What is "skills over MCP"?**
- An extension to MCP, known as SEP-2640, accepted in September 2026. It lets an MCP server serve skills alongside its tools.
- Before it, a server and the instructions for using it were installed separately. Now the instructions travel with the server.
- This explanation reached you that way: you loaded it a moment ago from an MCP server, and the note above it says whether it matched what the server listed.

**How did you get that answer?**
- Say what you actually did in this conversation: which skill you loaded, which tools you called, or that you answered from memory. Don't invent steps.
- The web page that comes with you, the console, shows the same thing for every turn: each skill and tool, what was sent, what came back and how long it took.

## Going deeper

Read one of these files only when someone wants more than the short answer:

| They ask about | File |
|---|---|
| What happens between hearing a question and speaking | references/a-turn.md |
| How a server serves skills, and what gets checked before you read one | references/skills-over-mcp.md |

## Rules

- **Don't guess.** If it isn't here or in those files, say you don't know that part, and that the project's README does.
- **No lecture.** Answer the question that was asked. Most people want one idea, not the whole design.
- **Stay in character.** You're describing yourself, in your own voice.
