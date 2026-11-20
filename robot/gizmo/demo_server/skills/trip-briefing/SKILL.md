---
name: trip-briefing
description: A short spoken briefing for someone about to travel - the time where they're going and how far ahead or behind it is, the weather there on the days of the trip, and what their money is worth there. Load it when someone says they are going to, flying to or visiting a place, or asks what to know before a trip.
license: Apache-2.0
compatibility: Uses the local_time, weather and convert tools of the MCP server that serves it.
---

# Trip briefing

Someone is about to travel. Give them a briefing they can take in by ear: three or four short sentences, then an offer.

## Steps

1. **Where and when.** Work out where they're going, and when if they said.
   - If they named only a country, use its capital and say that you did.
   - If they named nowhere, ask where, and stop there.
2. **Look it up.** Call these tools of the MCP server that serves this skill. They don't depend on each other, so call them together if you can. Your host may show each one with the server's name in front (weather as <server>_weather): use the name you have.
   - local_time(place): the time there now, and how far ahead of or behind home it is.
   - weather(place, days): the weather there now and on each of the next days.
     - A trip today or tomorrow, or no day given: leave days out.
     - A trip later this week, or on a named day: days 7.
     - A trip next week: days 14.
     - Later than that: days 16, which is as far as it reaches.
   - convert(100, the traveller's currency, the currency there): what their money is worth. Only for a place whose currency is different from the traveller's: a trip inside one country has no money step, so don't call it and don't mention money.
     - The traveller's currency is the one they've mentioned, else US dollars.
     - If the time result then shows home is somewhere that doesn't use dollars, say the rate is for US dollars and offer their own.
3. **Say it,** in this order:
   1. If they said what the trip is for, a few words about that.
   2. The time: "It's two in the morning there now, 14 hours ahead of you."
   3. The weather, in one sentence:
      - A trip today or tomorrow, or no day given: today and tomorrow together.
      - A later trip: the days they'll be there, found by their dates in the weather result. More than a week off, say it's a rough guide. If the forecast doesn't reach the trip, give the last days it has and say so.
      - Add what to pack only when it matters: rain or snow likely, hotter than 30 °C (86 °F), or colder than 5 °C (41 °F).
   4. The money, naming both currencies: "100 US dollars is about 16,000 yen."
   5. An offer: the flag, or something about the place.

## Rules

- **Say the briefing and nothing else.** Never say what you are about to do, what you looked up, what you left out or why, or which units or currency you chose. Your first words are the briefing's first words.
- **Units.** Use the ones the traveller uses at home, not the ones used where they're going: Fahrenheit and miles for someone who lives in the United States, even on a trip to Tokyo or Paris; Celsius and kilometres for someone who lives almost anywhere else. Say one, never both.
  - The local_time result names home. Take the traveller to live there unless they've said otherwise.
- **Rounding.** Temperatures to a whole degree, money to two significant figures, clock times to the nearest five minutes.
- **Never guess a live fact.** The time, the weather and the exchange rate come from the tools. If one tool fails, say that part isn't available and give the rest.
- **Don't read the tool results out.** They're your notes. Say only what the briefing needs.
