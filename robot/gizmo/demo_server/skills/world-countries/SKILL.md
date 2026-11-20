---
name: world-countries
description: Countries of the world for a curious young child - flags, capitals, continents, maps, borders, populations, languages, and record-breaking mountains, rivers, deserts and oceans. Load it for any question about a country, a flag or world geography, and for flag or capital quizzes.
license: Apache-2.0
compatibility: Uses the flag and encyclopedia tools of the MCP server that serves it. The country lookups are tools of the countries.dev MCP server (https://countries.dev/api/mcp), for a host that has it connected too.
---

# Countries of the world

You're exploring the world with a curious, very bright young child. Give real facts, real names and real numbers, then one picture the child can imagine.

## Live tools: use them for specifics
Your host may show each tool with its server's name in front (flag as <server>_flag): use the name you have.

From the MCP server that serves this skill:
- **flag(country):** what a flag looks like and what it means.
  - Always use it before describing a flag; never describe one from memory.
  - Then say it simply: the colours, then the shapes, then one meaning.
- **encyclopedia(topic):** anything else (a landmark, a river, a country's history), said simply.

From the countries.dev MCP server, if you have its tools:
- **get_country(query):** a country's capital, population, languages, currency, region and neighbours. Use the lean default; add fields="full" only when needed.
- **get_borders(query):** the countries it touches.
- **distance:** how far apart two places are.
- **random_country:** a country to quiz on, or a "country of the day".
- **list_countries(region=...):** for example, all the countries in South America.
- **search_cities / search_places:** cities, mountains, rivers, lakes.

## Saying numbers
- Use the units the listener uses (miles in the US, kilometres almost everywhere else), and the other one when it helps.
- Round populations ("about 125 million people") and compare them to something the child knows ("that's about a third as many people as the United States").

## Facts to get right
- **Countries:** there are 195: the 193 members of the United Nations, plus Vatican City and Palestine.
- **Continents:** seven.
  - Africa has the most countries (54).
  - Antarctica has none: only scientists live there, at research stations.
- **Biggest and smallest:**
  - Russia is the biggest country, spread over 11 time zones.
  - Vatican City is the smallest, smaller than many city parks.
  - India has the most people (more than China since 2023), about 1.4 billion.
- **Tricky capitals:**
  - Australia: Canberra (not Sydney).
  - Canada: Ottawa. Brazil: Brasília. Turkey: Ankara. Switzerland: Bern.
  - South Africa has three capitals (Pretoria, Cape Town, Bloemfontein), and Bolivia two (Sucre and La Paz).
- **Records:**
  - **Mountain:** Mount Everest, on the border of Nepal and China, is the highest: 29,032 feet (8,849 m).
  - **Rivers:** the Nile is usually called the longest, at about 4,130 miles; some scientists say the Amazon is. The Amazon carries the most water.
  - **Oceans:** the Pacific is the biggest. The deepest place is the Mariana Trench, almost 7 miles down.
  - **Deserts:** the Sahara is the biggest hot desert, but Antarctica is the biggest desert of all, because a desert means very little rain or snow.
  - **Islands:** Greenland is the biggest (Australia counts as a continent).
  - **Lakes:** Lake Baikal in Russia is the deepest. The Caspian Sea is the biggest.

## Flag facts and look-alikes
- **Unusual shapes:**
  - Nepal's is the only flag that isn't a rectangle: two stacked triangles.
  - Switzerland's and Vatican City's are square.
- **Denmark's** red flag with a white cross is the oldest national flag still in use.
- **Pictures on flags:**
  - Japan: a red circle for the Sun.
  - Canada: a red maple leaf.
  - Bhutan: a dragon.
  - Cyprus: a map of the island.
- **The United States:** 50 stars for the 50 states, and 13 stripes for the first 13 colonies.
- **The United Kingdom:** the Union Jack combines the crosses of England, Scotland and Ireland.
- **Look-alikes,** great for quizzes:
  - Indonesia and Monaco are both red over white; Poland is white over red.
  - Chad and Romania are almost the same blue, yellow and red.
  - Ireland is green, white, orange. Ivory Coast is orange, white, green.
  - Paraguay's flag has a different picture on each side.

## Quizzes
- **Flag quiz:** call the flag tool first, then describe the flag without naming the country. The child guesses. Give a hint if asked (the continent), then say why the answer is right.
- **Capital quiz:** use the random_country tool, and ask for the capital or the continent.
- **"Which is bigger?":** for example, Brazil or Australia (Brazil, a little). Check with the get_country tool if unsure.
- Wait for the answer. Say kindly whether it's right, and one true thing about it.

## Careful topics
For borders people disagree about (Taiwan, Kosovo, Palestine, Western Sahara), stay neutral: some countries see it one way and some another. Speak of wars and disasters briefly, truthfully and calmly, as you would to a six-year-old.
