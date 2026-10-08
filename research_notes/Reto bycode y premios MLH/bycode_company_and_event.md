# bycode (sponsor of "What Should Happen Next?") and the Hackathon TecNM Campus Saltillo 2026 / Road To Tech

Research date: 2026-10-08 (the day before the event). Network note: bycode.com.co, road2tech.com, mlh.com and tracxn.com were blocked by the egress proxy (EGRESS_BLOCKED / CONNECT 403), and `bycode.mx` does not resolve in DNS (getaddrinfo ENOTFOUND). The facts below therefore come from search-engine snippets of those pages, not from full-page reads. GitHub user lookups were also unavailable (session-scoped token).

## Who is bycode (identity, HQ, leadership, size, founding, LinkedIn)

### Takeaway
I could not confirm which "bycode" sponsors the Saltillo challenge. The most plausible candidate is **Bycode**, which calls itself a "Digital Product & Development Studio" at bycode.com.co. It writes in Spanish and lists its own SaaS products. I found no source tying it to Saltillo, Coahuila or Mexico. My confidence in this identification is low to medium.

### Cited Findings
- The website bycode.com.co presents Bycode as a "DIGITAL PRODUCT & DEVELOPMENT STUDIO" whose tagline is about turning ideas into technology. It says it designs web experiences, develops software and builds digital products "for companies that want to sell, automate and grow". — [Bycode](https://bycode.com.co/) (via search snippet)
- The same site claims "+8 años creando tecnología" (which implies founding around 2017–2018; my inference), "+150 proyectos desarrollados" and "3 productos propios". These figures are self-reported. — [Bycode](https://bycode.com.co/)
- The `.com.co` domain is Colombia's commercial second-level domain. That suggests a Colombian base but does not prove it. — [.co (second-level domain), Wikipedia](https://en.wikipedia.org/wiki/.co_(second-level_domain)); [Bycode](https://bycode.com.co/)
- `bycode.mx` does not resolve in DNS (WebFetch returned `getaddrinfo ENOTFOUND bycode.mx`). There is no live Mexican domain under that exact name. (Direct test, 2026-10-08.)
- Searches combining "bycode" with "Saltillo", "Coahuila" or "Monterrey", and searches of Saltillo job boards, found no Bycode office, no job posting and no Mexican LinkedIn page. — e.g. [OCC Saltillo desarrollador](https://www.occ.com.mx/empleos/de-desarrollador/en-saltillo-coahuila-mexico/), [Computrabajo developer Saltillo](https://mx.computrabajo.com/trabajo-de-developer-en-saltillo)
- **Unrelated companies with similar names (disambiguation):**
  - **BYCODE Group (bycode.biz)** builds fintech, Web3 and iGaming infrastructure. Tracxn's "Bycode" profile appears to describe this company, not the dev studio. — [bycode.biz](https://bycode.biz/); [Tracxn](https://tracxn.com/d/companies/bycode/__HJvoVltFocu-2OnIKJvKnGWVZZCiK3-H8Od6h3WNnlw)
  - **ByCode Technologies** is a web and Internet-marketing agency. Its Facebook page lists an Indian (+91) phone number. — [Facebook](https://www.facebook.com/bycodetechnologies/)
  - **bycode.dev** is a personal software blog. — [bycode.dev/about](https://www.bycode.dev/about/)
  - **GitHub user "bycode"** has about 110 repositories. Its owner is unknown and no link to the sponsor was found. — [github.com/bycode](https://github.com/bycode)
  - Also unrelated: "ByCode" jewellery, a "ByCode" brand on Archello, the Play Store developer "By-Code", Code México and BCode México. — [Facebook jewellerybycode](https://www.facebook.com/jewellerybycode/), [Archello](https://archello.com/brand/bycode), [Google Play](https://play.google.com/store/apps/dev?id=7393500706723628377)
  - Saltillo software firms with similar names, none of them bycode: **Fencode**, **VelvetCode Studio**, Agencia NEXA, Creatibot. — [Fencode Saltillo](https://www.fencode.dev/ubicaciones/saltillo), [VelvetCode](https://velvetcodestudio.com/desarrollo-de-software/estado/coahuila/)

### Inferences
- The sponsor may be the bycode.com.co studio, which could work remotely with Mexican clients or have a local contact at TecNM Saltillo. It could also be a small local firm with no indexed web presence. Treat the identification as unconfirmed until it is checked at the opening ceremony.
- Under either hypothesis, the sponsor is a small or medium software studio and not a large enterprise. Judges will likely value a working product that looks shippable over research novelty.

### Gaps
- Legal name (razón social), headquarters, founders, headcount and LinkedIn page: none found. LinkedIn and the company site could not be fetched.
- Whether "bycode" in the challenge is the same entity as bycode.com.co is unverified.
- No names of bycode judges or mentors for this event were found.

## What they build (products, services, industries, stack)

### Takeaway
If the sponsor is bycode.com.co, it is a custom-software studio that also sells its own SaaS. Its focus is business management (CRM/ERP), dashboards, automations, API integrations and event software, built on a mainstream web stack (React, Laravel, Django, PHP/MySQL, WordPress, Shopify).

### Cited Findings
- Listed services: software a medida (custom software), CRM / ERP, dashboards, automations, APIs and integrations, and SaaS platforms. The site says it "analyses each client's processes and converts them into digital tools, from internal platforms to full SaaS products". — [Bycode](https://bycode.com.co/)
- Technologies named on the site: React, Laravel, Django, WordPress, Shopify, PHP and MySQL. — [Bycode](https://bycode.com.co/)
- Its own products, described as "100% desarrollado por Bycode":
  - **BySuite**: "Todo tu negocio en un solo lugar". A modular platform that centralises clients, sales, documents, bookings, training and processes.
  - **ByEvents**: event software with registration, a dynamic QR code for check-in, and a dashboard of registrations, check-ins and conversion. The demo event is "Summit Digital 2026".
  - **GoBy.Link**: smart links, QR codes and analytics, e.g. "GoBy.Link/bycode".
  - — [Bycode](https://bycode.com.co/)
- The homepage dashboard figures (e.g. sales of $24.8M and 842 clients) look like product mock-up data, not company results. — [Bycode](https://bycode.com.co/) (search-engine summary; my caution)
- No named clients, case studies, industries served (e.g. automotive or manufacturing in Coahuila) or job postings were found.

### Inferences
- The challenge's "signals" (interactions, events, messages, state changes and user behaviour) map almost directly onto the data BySuite (CRM/ERP), ByEvents (registrations and check-ins) and GoBy.Link (click analytics) would produce. A demo that consumes CRM-style or event-style data, such as a lead going cold, a client not renewing, or an attendee who registered but did not check in, will likely resonate. So will an agent packaged as an add-on module for a SaaS platform.
- Their stack is conventional web (React plus a Laravel/Django backend). A solution exposed as a REST API or webhook that plugs into an existing CRM is likely to look more practical to them than a standalone notebook.

### Gaps
- No evidence of an ML or AI practice, an event-driven architecture, or tooling such as Kafka at bycode.
- No public client list or industry focus.

## Public statements about AI agents, event-driven systems, next-best-action

### Takeaway
I found no public bycode content on AI agents, next-best-action or decisioning. The challenge text is the only statement of their interest.

### Cited Findings
- Searches for "bycode" together with inteligencia artificial, agents or decisioning returned nothing relevant. — e.g. search results such as [vlex: primera fábrica de IA en México](https://vlex.com.mx/vid/presentan-primera-fabrica-ia-1094683563), which is unrelated.
- The site's stated value proposition, helping companies "sell, automate and grow", is commercial and operational. — [Bycode](https://bycode.com.co/)
- The challenge text (from the event platform, as supplied by the user) asks the agent to: process events; understand context; detect intent; estimate risk; recommend an action *or inaction*; justify it; choose the right moment; and state its confidence level.

### Inferences
- The wording ("siguiente mejor acción", risk, timing, confidence, the option of inaction) is classic Next-Best-Action / decision-intelligence vocabulary from CRM and customer-success work. That fits a company selling CRM/ERP and automation.
- Explicitly rewarding "inacción" and "momento adecuado" suggests judges will look for restraint and timing: not over-messaging the customer, and suppressing low-confidence actions. They will also look for explainability. A rubric-like output schema for each decision (intent, risk, action, rationale, timing, confidence) will probably map directly onto how they score.

### Gaps
- No published bycode judging rubric for this challenge was found.

## Past involvement with hackathons, TecNM, Road2Tech, universities

### Takeaway
I found no public record of bycode's past involvement with TecNM, Road To Tech or other hackathons.

### Cited Findings
- The ByEvents product demonstrates event management (registrations and QR check-in) for a "Summit Digital 2026". This is a demo dataset, not proof that bycode ran a real event. — [Bycode](https://bycode.com.co/)
- Searches combining Road To Tech or Road2Tech with sponsors or bycode found no sponsor list. — [Road To Tech 2026](https://www.road2tech.com/evento) (via snippets)

### Gaps
- Whether bycode provides judges, mentors or the platform for the event (e.g. ByEvents for registration) is unknown. The "5/12 equipos" counter on the event platform suggests a per-challenge cap of 12 teams. That is my inference and is unverified.

## The event: organizers, program, schedule, venue, rules, judging

### Takeaway
"Hackathon TecNM Campus Saltillo 2026" is an in-person MLH 2027-season event on 9–10 October 2026. Its public brand is "Road To Tech 2026". The student committee organizes it as a 24-hour hackathon for up to 45 teams. Challenges are revealed at the opening ceremony, and each team gets a 5-minute pitch to the jury on Saturday at 15:00.

### Cited Findings
- The MLH 2027 season schedule lists "Hackathon TecNM Campus Saltillo 2026", 9–10 October, Saltillo, Coahuila, MX, in person. A search summary noted a stray "Atlanta, Georgia" next to the entry, probably a listing error. — [MLH 2027 season](https://www.mlh.com/seasons/2027/events/) (via search snippet; page blocked)
- Road To Tech 2026 is "a 24-hour hackathon" on 9 and 10 October 2026 at TecNM Campus Saltillo, with a cap of 45 teams. It is organized by the Comité Estudiantil del TecNM Campus Saltillo. — [Road To Tech 2026](https://www.road2tech.com/evento)
- Format: "not a passive conference"; mentorship, food and a jury at the end; "24 horas, contadas hora por hora". — [Road To Tech 2026](https://www.road2tech.com/evento)
- Schedule highlights: arrival and check-in on Friday at 12:30; challenges are not revealed beforehand and are announced at the opening ceremony; Saturday at 15:00 each team has **5 minutes before the jury** to defend its project. — [Road To Tech 2026](https://www.road2tech.com/evento)
- Venue: TecNM Campus Saltillo (Instituto Tecnológico de Saltillo), Blvd. Venustiano Carranza, Tecnológico 2400, Saltillo, Coahuila. — [Road To Tech 2026](https://www.road2tech.com/evento); [Saltillo Institute of Technology, Wikipedia](https://en.wikipedia.org/wiki/Saltillo_Institute_of_Technology)
- Separate events, not to be confused with this one: InnovaTecNM 2026 local stage (May 2026, which included HackaTec) — [Vanguardia](https://vanguardia.com.mx/coahuila/saltillo/arranca-innovatecnm-en-el-campus-saltillo-semillero-de-innovacion-MJ20830936); Hackathon Mujeres STEM 2026 (municipal, CONALEP) — [saltillo.gob.mx](https://saltillo.gob.mx/impulsa-saltillo-talento-de-jovenes-con-hackathon-mujeres-stem-2026/).
- The MLH listing and the Road To Tech page share the same dates, campus and city, so they very likely describe the same event. Neither source states the link explicitly. — [Road To Tech 2026](https://www.road2tech.com/evento); [MLH 2027 season](https://www.mlh.com/seasons/2027/events/)
- I found no independent press coverage of Road To Tech 2026. The organizer page is the only source.

### Inferences
- MLH member events normally follow the MLH Code of Conduct and expect work to be created during the event. Standard MLH practice allows open-source libraries and APIs but not pre-built projects. I did not verify this for this specific event, so check the rules at check-in.
- A 5-minute pitch means the demo must show the full decision loop quickly: event in, then intent, risk, action or inaction, rationale, timing and confidence out. A live demo plus a decision-trace view is likely to score better than architecture slides.

### Gaps
- Official judging criteria, the prize for the bycode challenge, team-size limits, eligibility and rules on pre-existing code: not found (road2tech.com and mlh.com are blocked; snippets did not include them).
- Full sponsor list and other challenges: not published before the opening ceremony.
- Identity of the jury and mentors.
