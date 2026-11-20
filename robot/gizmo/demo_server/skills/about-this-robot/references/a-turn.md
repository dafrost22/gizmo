# What happens in one turn

From someone speaking to you answering. Explain only the steps that were asked about.

1. **Hearing.** The microphones in your head stream sound to the voice model, Amazon Nova Sonic. It turns speech into words and notices when the person has stopped.
2. **Is it for me?** In wake-word mode a turn needs your name, unless it follows something you just said. Speech that wasn't for you is dropped, and its words aren't kept.
3. **Turning to look.** The microphone array reports which direction the voice came from, and your head turns that way.
4. **Handing over.** The voice model has one tool, called think. It passes what it heard to the brain and waits.
5. **Thinking.** The brain is a second agent. It reads three things:
   - Your personality's system prompt: who you are.
   - A fixed section on how you work: speak for the ear, and how to ask for a gesture.
   - The list of skills: a name, a description and an address for each. Not their contents.
6. **Loading a skill,** if the question is one a skill covers. The skill's text arrives as the result of a tool call, so it joins the conversation from then on.
7. **Calling tools,** if the answer needs live facts. Each call goes to an MCP server and the result comes back as text.
8. **Answering.** The brain writes the words and, in square brackets, any gestures. The gestures go to your body, and the words go back to the voice model, which says them.

## What it costs in time

- A plain answer: a second or two.
- A skill and a tool: a few seconds more, mostly the tool waiting on the internet.
- If the model is too slow to start speaking, a second model takes over the same turn, with the same conversation.

## What a personality changes

A personality is a name, a system prompt, a voice, and its own choice of servers, tools and skills. Nothing is shared between personalities. Switching rebuilds the brain in about two seconds.
