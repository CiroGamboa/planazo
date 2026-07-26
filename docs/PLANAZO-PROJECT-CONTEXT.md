# Project Context — Barcelona Event Discovery Agent

Our project addresses a common problem for students and young professionals living in a city like Barcelona: there are many interesting events happening every day, but the information is fragmented across too many sources. A person may need to check Meetup, Eventbrite, Instagram pages, city/government websites, cultural venues, university pages, and private community posts just to decide what to do in the evening. This creates information fatigue: the user spends more time searching, filtering, and comparing events than actually choosing a plan.

We want to build an agentic system that centralizes event discovery for Barcelona. The user should be able to ask something like: “Find me interesting events in Barcelona today after 18:00,” or “Show me tech and networking events this weekend,” and the system should gather candidate events from selected sources, extract the key details, filter them according to the user’s preferences, and present a curated list. If the user chooses an event, the system can prepare a Google Calendar entry and optionally invite friends by email, but only after explicit user approval.

The main technical challenge is extracting useful event information from messy, semi-structured sources. Some event platforms expose structured APIs, but many relevant Barcelona plans are announced through Instagram accounts or informal posts. A core goal of the project is therefore: given an Instagram account link or post link, the system should attempt to extract possible event announcements, including title, date, time, location, price, category, source link, and confidence score. The system should not simply scrape everything blindly; it should identify whether a post actually contains an event and whether the extracted data is reliable enough to show to the user.

This project is agentic because the system does not follow one fixed script. It observes the user request, decides which source tools to call, extracts and validates event candidates, reasons about missing or conflicting details, ranks the results, and decides whether it needs another tool call or whether it can stop and answer. This follows the course idea of an agentic loop: observe → reason → act → verify → repeat. The class material also emphasizes that tools are functions exposed to the model with a name, description, and parameter schema, and that the model chooses when and how to call them.

For this stage of the course, we will not use agent frameworks such as LangChain, LangGraph, CrewAI, or PydanticAI. The course plan places agent frameworks later in the second half of the course, while the current homework focuses on hand-built tools, loops, guards, and human-in-the-loop design.  Instead, we will implement the agent loop ourselves in Python. This helps us understand the mechanics directly: tool schemas, tool routing, loop stopping conditions, error handling, and approval gates.

Our first version will use a small number of tools designed by us. For example:

- `search_eventbrite_events(city, date, category)`
- `search_meetup_events(city, date, category)`
- `extract_events_from_instagram_link(url)`
- `normalize_event(raw_event)`
- `rank_events(events, user_preferences)`
- `create_calendar_event_draft(event_id)`
- `confirm_and_create_calendar_event(event_id)`

At least one of these tools will call a real API or persist data. For example, the Google Calendar tool can create a draft/staged event, while the event database can be stored locally in JSON or SQLite. This matches the homework requirement that the agent should have at least two tools designed by the team, with at least one real tool that touches a file, API, or persistent state.
The system will include explicit safety boundaries. Reading public event information and preparing a calendar draft are reversible or low-risk actions. However, actually creating a calendar event with invited guests, or sending email invitations, is an external action that affects other people. Therefore, this action will require a chat-based approval gate. This follows the class principle that irreversible actions need human oversight, while reversible or read-only actions can remain ungated.

The system will also include error handling. Event extraction can fail because a page is unavailable, an Instagram post has no date, an API returns incomplete data, or the model extracts an impossible time. These failures should not be silently treated as valid events. Instead, the tool should return a separate error state, such as `missing_date`, `unsupported_source`, `low_confidence_extraction`, or `api_error`. The current homework specifically requires that one tool error be caught and surfaced to the loop as a distinct branch.
A central challenge is that web pages and social posts are untrusted input. An Instagram caption or event page may contain irrelevant text, misleading information, or even prompt-injection-like instructions. The course material warns that retrieved text and prompts can become mixed in the model’s context, so the system must treat all tool output as data, not as instructions. For this reason, our extraction tool will return structured fields only, and the agent will not obey instructions found inside scraped content.

The expected output of the MVP is a curated event list for a user-specified date, time window, and interest category. Each event should include the title, time, location, short description, category, source, confidence score, and reason why it was recommended. The user can then select one event and approve adding it to Google Calendar.

A possible user flow is:

1. User asks: “Find me interesting tech or cultural events in Barcelona today after 18:00.”
2. Agent calls event-source tools.
3. Agent extracts and normalizes candidate events.
4. Agent filters events by time, location, and preferences.
5. Agent presents a ranked list.
6. User chooses one event.
7. Agent prepares a calendar event draft.
8. User explicitly confirms.
9. Agent creates the Google Calendar event and optionally invites friends.

The final goal is not to build a generic scraper, but a safe and useful local event-discovery agent. The project combines tool use, context assembly, structured extraction, ranking, memory of user preferences, error handling, and human approval. These are all core capabilities discussed in the course: feedback loop, tool use, context assembly, delegation, human-in-the-loop, and levels of autonomy.