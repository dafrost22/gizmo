# Skills over MCP, in more detail

The extension is SEP-2640, "Skills", with the identifier io.modelcontextprotocol/skills. It was accepted in September 2026. Explain only the part that was asked about.

## What the server does

- Each file of a skill is an MCP resource with an address that starts skill://, such as skill://quiz-night/SKILL.md.
- The server answers a request called skills/list. For each skill it gives the name and description, and every file with its size and a fingerprint of its contents (a SHA-256 digest).
- A second request, skills/get, returns one skill by its address.

## What the host does

The host is the application the model runs in. Here, that's the robot.

1. **Listing costs nothing.** When a server connects, the host asks for the list and fetches no files. Only names and descriptions go into the model's prompt.
2. **Loading is checked.** When the model asks for a skill, the host fetches its SKILL.md and compares it with the listing: the size, the fingerprint, and the name and description. If they differ, it asks the server once more in case the skill was just edited, and otherwise refuses.
3. **The model is told where the text came from.** The skill arrives labelled with its server's name, as reference material from that server. It isn't an instruction from the developer or from the person talking.
4. **Other files stay inside the skill.** The model can read a skill's extra files only after loading that skill, and only from the server that served it.
5. **A skill can't grant itself permissions.** A skill may list tools it would like to be allowed. The host ignores that list: a personality's tools are chosen by a person, on the console.
6. **Names don't identify a skill.** Two servers can both serve a skill called "refunds". A skill is its server plus its address.

## Why it matters

- **One install.** Connect a server and you get its tools and the know-how for using them. When the server updates a skill, every host gets the new one.
- **Small prompts.** A model can have dozens of skills on offer and pay only for the ones it loads.
- **Checkable.** The fingerprints mean the host can tell that what it loaded is what the server said it would serve.

## What to be careful of

A skill is text that a model will follow, written by whoever runs the server. Only connect servers you trust.

## The three skills that show it off

They're served by the same demo server as this one:
- trip-briefing: a procedure that runs three tools and says how to give the answer.
- quiz-night: a short SKILL.md whose question rounds are separate files, read only when a round is chosen.
- about-this-robot: this one, with its details in reference files like the one you're reading.
