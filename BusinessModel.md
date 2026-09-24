# Rytm: Business Model (Lean Canvas)

This business model is based on a **two-sided marketplace** structure, connecting event organizers with local attendees. 

## 1. Target Segments (Who are we building this for?)

*   **B2C (App Users):** People with an active social life who experience FOMO (Fear Of Missing Out) and are tired of scrolling through dozens of Telegram channels to find out what's happening. *The core app must always remain free for them.*
*   **B2B (Event Organizers):** Ranging from student organizations and local stand-up clubs to large promoters and art centers (like !FESTrepublic or Jam Factory). Their main pain point is that traditional Instagram targeted ads are becoming too expensive, and organic reach in Telegram is dropping.

## 2. Value Proposition (Why are we better?)

*   **For Users:** "All city events on one live map in your smartphone. No spam—only what's happening nearby and matches your vibe."
*   **For Organizers:** "Direct access to a warm audience that is actively looking for a place to hang out tonight. No need to set up complex ad campaigns."

## 3. Revenue Streams (Monetization)

At launch, the focus is entirely on user acquisition. Once the platform reaches 3,000–5,000 active users, the following monetization streams will be activated:

*   **Premium Placement (Freemium for B2B):** Adding a basic event is free (standard grey map pin). However, for $10–$20, organizers can buy a "Priority" boost: their pin becomes neon and pulsates, the event is pinned to the top of the list, and users within a 2 km radius receive a push notification.
*   **Affiliate Programs (Ticketing/CPA):** Rytm doesn't sell tickets directly. Instead, a "Buy Ticket" button redirects users to platforms like Concert.ua or Gastroli. Rytm earns a 5–15% affiliate commission for every ticket purchased through the app via API tracking.
*   **Local Business Integration (Phase 2):** When people attend an event, they usually want to grab a coffee or a cocktail before or after. Rytm can charge nearby bars and cafes to be highlighted as "Recommended places near this event."

## 4. Acquisition Channels (Go-To-Market Strategy)

*   **B2C:** Guerrilla marketing on university campuses, TikTok/Reels showcasing Lviv's hidden locations through the app's map, and memes about the struggle of finding good parties.
*   **B2B (Direct Sales):** Manually collecting a database of 50 local organizers, reaching out via Instagram Direct, and offering to add their first events to the map for free to demonstrate the platform's value.

## 5. Cost Structure (Where the budget goes)

*   **Infrastructure:** Cloud hosting for the PostgreSQL/PostGIS database (requires slightly more RAM for spatial queries) and the Django REST API servers.
*   **Map APIs:** The biggest hidden cost. Services like Mapbox or Google Maps charge once you exceed their free tiers (e.g., Mapbox offers 50,000 free map loads per month, after which it becomes paid).
*   **App Store Fees:** A one-time $25 fee for Google Play Developer Console and a $99/year fee for the Apple App Store.

---

> **Key Focus for MVP (Minimum Viable Product):** 
> Do not try to build the perfect B2B dashboard right away. In the first few months, you will have to manually parse and upload city events via the Django Admin panel. Users need to open the map and see that it is "alive." Once you have a solid user base, organizers will start coming to you organically.
