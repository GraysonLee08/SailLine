# Product Requirements Document
## SailLine — Real-Time Sailing Race Router

**Version:** 1.2
**Platform:** Web App (+ optional hardware telemetry module)
**Target Users:** General public — competitive sailors, race crews, coaches
**Business Model:** Freemium (free tier + paid subscription; hardware add-on in roadmap)
**Primary Market:** United States — Great Lakes focus at launch

---

## Changelog

| Version | Date | Summary |
|---|---|---|
| 1.2 | 2026-04-30 | Added GLERL/GLCFS surface current data (Section 7), NDBC buoy observations for forecast validation (Section 7), expanded Pi hardware module with dedicated IMU sensor for heel/pitch/trim (Section 11), added AI Tactician design philosophy (Section 5.2), updated tier gates to reflect modeled currents for Pro tier |
| 1.1 | 2026-04-28 | GCP architecture finalized, cost model validated, build sequence established |
| 1.0 | 2026-04-26 | Initial PRD |

---

## 1. Problem Statement

Competitive sailors lack an accessible, unified tool that combines real-time wind and weather data, live competitor tracking, boat-specific performance modeling, and intelligent tactical advice to make optimal routing decisions before and during a race. Existing solutions use static generic polars, single deterministic forecasts, and provide no AI-driven guidance. The Great Lakes racing community — including MORF club racing and the Chicago Yacht Club Race to Mackinac — is a large, underserved audience.

---

## 2. Goals

- Deliver pre-race planning and in-race routing using probabilistic weather and ML-enhanced polars
- Provide an AI tactical advisor that translates routing math into plain-language recommendations
- Surface competitor positions via AIS for tactical situational awareness (Pro/Team)
- Support corrected time estimates across major US handicap systems
- Record raw GPS (and eventually instrument) telemetry to enable post-race AI analysis
- Lay groundwork in v1 for a hardware telemetry module (Pi + NMEA) in v2/v3
- Operate sustainably: positive margin at realistic subscriber volumes

---

## 3. User Personas

**Primary — The Club Racer**
Races weekends on Lake Michigan (e.g., MORF fleet), owns or crews a Beneteau 36.7, J/105, or similar. Wants smarter tactical decisions without hiring a professional weather router. Likely starts on Free, converts to Pro.

**Secondary — The Distance Racer**
Participates in multi-day events like the Chicago-Mac. Needs multi-day weather routing and wave data. Pro or Hardware tier.

**Tertiary — The Instrument Integrator**
Technical sailor or boat owner who installs the SailLine Pi module for real-time instrument telemetry, current detection, and learned polars. Hardware tier. Most likely a boat owner (vs. crew) given the hardware investment.

---

## 4. Tier Structure & Pricing

| Tier | Monthly | Annual | Core Gates |
|---|---|---|---|
| **Free** | $0 | — | Pre-race planning · All boat classes · No AIS · 24hr weather · Account required |
| **Pro** | $15/mo | $149/yr | In-race routing · AIS competitors · 7-day weather · Probabilistic ensemble routing · AI tactical advisor · GLCFS modeled currents · Handicap · GPS track recording · Post-race AI analysis |
| **Hardware** | $25/mo | $249/yr | Everything in Pro + Pi telemetry ingestion · True local wind routing · Measured current detection (GPS SOG − hull speed) · Instrument-enhanced post-race analysis · ML polar learning |

> Hardware tier requires the SailLine Pi Module (~$200 one-time hardware purchase, sold separately).
> Hardware kit is DIY-documented for technical users; pre-assembled kit available for purchase.
> Break-even: 22 Pro subscribers. Annual plans save ~17%.

---

## 5. Routing Strategy

### 5.1 Algorithm: Isochrone + Probabilistic Ensemble

Isochrone routing is the correct algorithmic foundation — it is the industry standard used by professional tools (Expedition, PredictWind) because it is computationally efficient, real-time capable, and produces optimal results given input data quality.

The game-changer is **what you feed into the isochrone engine**, not replacing it.

**Why not other algorithms:**

| Algorithm | Assessment |
|---|---|
| A* / Graph search | Faster to implement but produces suboptimal routes vs. isochrone |
| Genetic / Evolutionary | Excellent quality but too slow for real-time recalculation |
| Pure RL / Deep Learning routing | Still academic research; no production-ready consumer implementation |

**SailLine's differentiated three-layer approach:**

**Layer 1 — Probabilistic / Ensemble Routing** *(the real differentiator)*
Instead of routing on a single deterministic wind forecast, run the isochrone engine across multiple NOAA ensemble forecast members simultaneously. Present the sailor with:
- The optimal route across the majority of forecast scenarios
- A confidence band: "This route is optimal in 8 of 10 ensemble models"
- A risk-adjusted alternative: "Conservative route if the forecast left shift doesn't materialize"

No consumer sailing app currently does this well. It is the methodology used by professional weather routers for offshore racing.

**Layer 2 — ML-Enhanced Polars** *(accuracy advantage)*
Replace static generic class polars with a neural network trained on real GPS + instrument data collected from actual races. Over time, SailLine learns the actual performance curves of specific boats in real conditions — accounting for bottom fouling, sail wear, crew weight, local sea state, and other factors generic polars ignore. Requires instrument telemetry data (see Section 11). Launches with generic polars; ML polars activate as instrument data accumulates.

**Layer 3 — AI Tactical Advisor** *(user experience differentiator)*
Claude API layered on the routing engine output, translating math into plain-language real-time tactical guidance:
- "Wind is forecast to shift 12° left in 18 minutes. Tack now to be on the lifted tack at the layline."
- "Three ensemble models show a significant pressure shift mid-lake. The conservative route hugging the Michigan shore reduces risk."
- "You are currently sailing 6° below optimal VMG. Head up slightly."

This is the feature non-expert sailors will feel most viscerally. It removes the expertise barrier to using routing data.

### 5.2 AI Tactician Design Philosophy

In competitive sailing, the tactician is the strategic brain of the boat. While the helmsman focuses on steering and the crew focuses on boat speed, the tactician looks "outside the boat" to determine the fastest path to the finish line. SailLine's AI advisor is modeled on this role. Its four primary responsibilities map to available data as follows:

**Wind Strategy** — SailLine's strongest area. HRRR provides 3km-resolution wind fields updated hourly. The isochrone engine computes optimal headings based on forecast shifts. The advisor can identify headers (shifts forcing the boat away from the mark) and lifts (shifts allowing the boat to point closer to the mark) from forecast data and recommend tack timing. What SailLine *cannot* do is see real-time puffs and lulls on the water — that remains the crew's eyes. However, when Hardware tier telemetry is available, SailLine can detect divergence between forecast wind and observed conditions (via onboard instruments), flagging when the model is wrong.

**Fleet Positioning** — AIS provides competitor positions, headings, and speeds. The advisor can flag proximity ("boat X is 0.3nm to windward on the same tack"), convergence patterns, and relative positioning to the next mark. What SailLine *cannot* reliably detect is dirty air / wind shadow — that operates at ~50m scale, well below HRRR resolution and AIS refresh rates. However, it can warn about close-quarters situations where dirty air is likely.

**Navigation & Course Awareness** — Course marks are user-defined, so layline calculations are straightforward. The advisor calls approaching laylines, leg transitions, and mark roundings. Racing Rules of Sailing (RRS) adjudication is out of scope — AIS refresh rates (3–5 min) are too slow for real-time right-of-way determinations, and the crew knows when they're in a port-starboard situation.

**Communication Style** — The advisor's output must match how a real tactician communicates: short, decisive, time-sensitive calls. Not paragraphs of analysis. Timing and tone are as important as content. Examples: "Tack in 30 seconds — layline in 200m." Not: "Based on the current wind forecast and your VMG calculations, it appears that initiating a tack maneuver in approximately 30 seconds would be optimal."

### 5.3 Current-Aware Routing *(new in v1.2)*

Lake Michigan surface currents can exceed 0.5 knots in certain conditions — enough to materially affect race outcomes, especially on distance races like the Mac. No consumer sailing app currently factors Great Lakes currents into routing.

**v1 (Pro tier):** Ingest NOAA GLERL GLCFS modeled surface current forecasts (NetCDF via THREDDS, updated every 12 hours, 120-hour forecast horizon). Overlay current vectors on the isochrone engine as an additional velocity field. This gives all Pro users current-adjusted routing without any hardware.

**v2 (Hardware tier):** Measure actual water current by computing the vector difference between GPS SOG/COG and hull speed/heading from the paddle wheel and compass. This measured current is more accurate than GLCFS at the boat's specific location and updates continuously. When available, it overrides the GLCFS modeled current for the boat's immediate vicinity while the model is still used for areas ahead.

---

## 6. Core Features

### 6.1 Pre-Race Planning *(all tiers)*
- Place race marks on an interactive map
- Pre-loaded marks: Chicago-Mac course coordinates + CYC Monroe Station start line. All other venues use user-defined marks. Community mark library is a v2 feature.
- Select boat class from polar library
- Fetch probabilistic wind forecast for race area and time window (24hr Free, 7-day Pro/Team)
- Generate optimal route with confidence visualization across ensemble models
- Leg-by-leg predicted speed based on polars + forecasted wind
- Surface current overlay from GLCFS (Pro tier, when available)

### 6.2 In-Race Routing *(Pro/Hardware only)*

| Mode | Use Case | Recalculation | AIS Refresh |
|---|---|---|---|
| **Inshore / Buoy** | Club racing, 1.5–4hr races | Every 2–3 min | Every 3 min |
| **Distance** | Mac race, overnighters | Every 10–15 min | Every 5 min |

- Probabilistic re-routing on each cycle using latest cached ensemble data
- Current-adjusted routing using GLCFS surface current vectors (Pro); measured current override when Hardware telemetry available
- AI tactical advisor updates in sync with routing recalculation
- Own position via browser Geolocation API (Hardware tier: Pi GPS module)
- AIS competitors overlaid on map
- Wind shift alerts when route materially changes
- Forecast divergence alerts when NDBC buoy observations or Hardware-tier onboard wind readings differ significantly from the HRRR forecast model
- ETA to next mark based on current VMG
- Raw GPS recorded every 30 seconds: timestamp, lat/lon, speed, heading, wind snapshot

### 6.3 Boat Polar Library *(all tiers)*

**Required at launch:**

| Class | Priority |
|---|---|
| Beneteau First 36.7 | P0 — required |
| J/105 | P1 |
| J/109 | P1 |
| J/111 | P1 |
| Farr 40 | P1 |
| Beneteau First 40.7 | P1 |
| Tartan 10 | P1 |
| Generic PHRF/ORC mid-range | P1 |

Generic polars at launch. ML-learned polars replace them per-boat as instrument telemetry data accumulates (v2+).

### 6.4 AIS Competitor Tracking *(Pro/Hardware only)*
- Server-side polling per geographic race corridor; cached and shared across concurrent users
- Polling gated to active Pro/Team sessions only
- Competitor positions on map; lifted vs. headed tack indicator; distance-to-mark estimate
- Mid-lake coverage warning for offshore legs
- Mac Race note: CYC uses YB Tracking (proprietary, closed API); standard AIS only

### 6.5 Handicap & Jurisdiction Support *(Pro/Hardware only)*
Supported: **PHRF, ORC, ORR-EZ, IRC, MORF**
Corrected time estimates alongside elapsed time. Selection saved to user profile.

### 6.6 Post-Race AI Analysis *(v2 — instrument data enhances quality)*

**v1 prerequisite (in scope now):**
Store raw GPS positions every 30 seconds: timestamp, lat/lon, speed, heading, wind snapshot. Never pre-process — derive segments (tack detection, leg identification) at analysis time, not record time. Raw data cannot be reconstructed retroactively.

**v2 analysis pipeline:**
1. Replay GPS track overlaid against re-simulated optimal route on map
2. Re-run isochrone at each recorded position using actual wind conditions
3. Compute time delta between actual and optimal at each decision point
4. Identify: missed lifts, wrong tack, suboptimal laylines, VMG inefficiency
5. Pass structured analysis to Claude API
6. Return plain-language performance summary with ranked improvement recommendations

**Instrument data enhancement (Hardware tier):** When Pi telemetry is available, analysis gains: actual VMG vs. polar target, tacking angle efficiency, sail trim correlation with speed (via heel data), water current impact — a qualitative leap over GPS-only analysis.

### 6.7 Unified Dashboard UI
- Split-screen: interactive map (primary) + data panel (sidebar)
- Map: route overlay with ensemble confidence band, wind barbs, current vectors, wave indicators, AIS positions, own position
- Data panel: AI tactical advisor output, recommended heading, predicted speed, VMG, wind, current, ETA, corrected time
- **v1:** Desktop-optimized (1280px+)
- **v1 stretch:** Tablet/cockpit responsive (768–1024px landscape)

---

## 7. Weather, Environmental & Observational Data

| Data Type | Source | Update Frequency | Tier | Status |
|---|---|---|---|---|
| Short-term wind (0–48hr) | NOAA HRRR (3km resolution) | Every 1 hr | All | v1 — implemented |
| Multi-day deterministic wind | NOAA GFS (0.25° resolution) | Every 3–6 hrs | All | v1 — implemented |
| Ensemble forecasts (probabilistic) | NOAA GEFS (21 members) | Every 6 hrs | Pro+ | v1.5 |
| **Surface currents (modeled)** | **NOAA GLERL GLCFS (NetCDF via THREDDS)** | **Every 12 hrs (120-hr forecast)** | **Pro+** | **v1 — new** |
| **Surface water temperature** | **NOAA GLERL GLSEA (satellite-derived)** | **Daily** | **Pro+** | **v1.5 — new** |
| Wave height & period | NOAA Great Lakes Wave Watch III | Every 3 hrs | Pro+ | v1.5 |
| **Buoy observations (wind, wave, temp)** | **NOAA NDBC Great Lakes buoys** | **Real-time (varies by station)** | **Pro+** | **v1 — new** |
| Marine alerts | NOAA NWS Marine Forecast | Every 15 min | All | v1 |

### 7.1 GLERL / GLCFS Surface Currents *(new in v1.2)*

The NOAA Great Lakes Coastal Forecasting System (GLCFS) is an experimental set of 3D hydrodynamic models (FVCOM-based) that predict currents, water temperatures, and waves across all five Great Lakes. The next-generation version uses high-resolution unstructured grids (30m–2km resolution) and forecasts out to 120 hours, updated every 12 hours.

**Why this matters for racing:** Lake Michigan surface currents can exceed 0.5 knots. On a 333-mile Chicago-Mac race, even a 0.3-knot current advantage sustained over 12 hours is roughly 3.6 nautical miles — easily the difference between first and fifth in a competitive fleet. No consumer sailing app currently incorporates Great Lakes current data into routing.

**Data access:** NetCDF files via GLERL's THREDDS server (two-week rolling window). Same parsing pipeline as existing GRIB2 weather data (Python + xarray/netCDF4). Free, public NOAA data — no API key or cost.

**Integration approach:** Extract surface-layer u/v current components, cache to Redis alongside wind data, inject as an additional velocity field in the isochrone engine. Current vectors are added to boat velocity during route propagation.

### 7.2 NDBC Buoy Observations *(new in v1.2)*

NOAA's National Data Buoy Center maintains a network of fixed buoys and coastal stations across the Great Lakes that report real-time wind speed/direction, wave height/period, air/water temperature, and barometric pressure.

**Primary use: forecast validation.** Compare NDBC observations against the HRRR wind forecast at the buoy's location. When observed winds diverge significantly from the model (e.g., >5 knots or >20° directional shift), trigger a forecast confidence alert in the UI. The AI tactical advisor can incorporate this: "Buoy 45007 (south-central Lake Michigan) is reporting 18kt NW — 6kt above the HRRR forecast for this area. Expect stronger wind ahead."

**Secondary use: ground truth for post-race analysis.** During replay, overlay actual buoy readings against the forecast used for routing decisions to identify where the model was wrong.

### 7.3 GLERL Surface Water Temperature *(v1.5)*

Satellite-derived lake surface temperature maps (GLSEA product) updated daily. Thermal gradients drive lake breeze formation — when warm air over the land meets cool air over the water, a secondary wind circulation develops near shore that can significantly alter wind patterns. This data helps the AI advisor warn about potential lake breeze effects near the Michigan and Wisconsin shorelines, particularly relevant for afternoon inshore races.

---

## 8. GPS & Position Tracking

| Position | Source |
|---|---|
| Own boat | Browser Geolocation API (sub-second, accurate) |
| Competitors | Datalastic AIS (server-side, cached per race zone) |
| Own boat — enhanced | Pi telemetry module (v2, see Section 11) |

---

## 9. AIS Provider: Datalastic

Self-serve, commercial use permitted, bulk endpoint (100 vessels/call, 1 credit/vessel). MarineTraffic ruled out (enterprise-only since Jan 2025). YB Tracking ruled out (closed system).

| Stage | Plan | Credits | Cost |
|---|---|---|---|
| Launch (<50 Pro users) | Starter | 20k/mo | ~$220/mo |
| Growth (50–150) | Experimenter | 80k/mo | ~$330/mo |
| Scale (150+) | Developer Pro+ | Unlimited | ~$750/mo |

---

## 10. Pricing Model

> Hardware tier subscribers ($25/mo) generate better per-user margin than Pro. Hardware kit (~$200) sold at modest margin over BOM, or DIY-documented free to drive Hardware tier subscriptions.

| Subscribers | Revenue | Infrastructure + Stripe | Net Margin |
|---|---|---|---|
| 22 Pro | $330 | ~$320 | ~$10 (break-even) |
| 50 Pro | $750 | ~$360 | +$390/mo |
| 100 Pro | $1,500 | ~$563 | +$937/mo |
| 100 Pro + 20 Hardware | $2,000 | ~$600 | +$1,400/mo |
| 500 Pro + 50 Hardware | $8,750 | ~$1,200 | +$7,550/mo |

---

## 11. Hardware Telemetry Module *(v2 roadmap — architecture planned in v1)*

### Concept
An optional Raspberry Pi-based hardware module that connects to the boat's existing instrument network via NMEA 0183 or NMEA 2000 and streams real-time telemetry to SailLine. This is a significant product expansion — not a trivial feature.

### Why It's Game-Changing
Professional sailing teams use full instrument telemetry systems (B&G Zeus, Expedition + NMEA 2000) costing thousands of dollars. The Pi module delivers equivalent data collection for ~$150–200 in hardware, integrated with SailLine's cloud analysis. No consumer app offers this at this price point.

### Data Captured

| Signal | Source | NMEA Sentence | Value |
|---|---|---|---|
| GPS position | NMEA or onboard GPS | $GPGGA / $GPRMC | More accurate than phone GPS |
| Boat speed through water | Paddle wheel via NMEA | $IIVHW | Critical for current detection |
| True wind speed & direction | Masthead unit via NMEA | $IIMWD | More accurate than forecast for immediate routing |
| Apparent wind | Masthead unit via NMEA | $IIMWV | Sail trim analysis |
| Magnetic heading | Compass via NMEA | $IIHDG | Tacking angle analysis |
| **Heel angle** | **Dedicated IMU (I2C)** | **N/A — direct sensor** | **Sail trim efficiency, performance optimization** |
| **Pitch / trim** | **Dedicated IMU (I2C)** | **N/A — direct sensor** | **Wave impact on speed, weight distribution** |
| **Roll rate** | **Dedicated IMU (gyroscope)** | **N/A — direct sensor** | **Tack/jibe detection, motion severity** |

### 11.1 Dedicated IMU Sensor *(new in v1.2)*

Many boats in the MORF fleet lack electronic heel sensors — their instrument packages are minimal or analog. A dedicated IMU (Inertial Measurement Unit) connected directly to the Raspberry Pi via I2C provides heel, pitch, and motion data independent of the boat's existing instruments.

**Recommended hardware:** BerryIMU v3 (LSM6DSL accelerometer/gyroscope + LIS3MDL magnetometer, ~$30) or MPU9250 (9-DOF, ~$4–15). Both connect via I2C and provide 3-axis acceleration, 3-axis gyroscope, and 3-axis magnetometer at sample rates of 100Hz+. A Kalman or complementary filter fuses accelerometer and gyroscope data to produce stable heel and pitch angles.

**What the IMU enables:**

- **Heel angle** — Directly actionable by the AI advisor: "You're heeling 22° in 14 knots — ease the main or move crew to the rail, you're losing ~0.4kt to excessive heel." Optimal heel varies by boat class and wind speed; combined with polar data, the system can compute the speed penalty of over-heeling.
- **Pitch / fore-aft trim** — Correlates with wave-induced speed loss. Excessive pitching indicates the boat is fighting waves rather than cutting through them. The advisor can recommend weight-forward or weight-aft adjustments.
- **Roll rate (gyroscope)** — High-frequency roll rate data enables automatic tack and jibe detection (sharp roll reversal signatures). This replaces heuristic-based detection from GPS heading changes, which is less reliable in light air or sloppy seas. Roll rate also quantifies sea state severity at the boat's location — a direct input the advisor can use for safety warnings.
- **Magnetic heading (magnetometer)** — Provides a heading reference independent of NMEA compass, useful as backup or for boats without electronic compasses.

**Calibration:** The IMU must be calibrated on installation. Accelerometer calibration is done by orienting each axis toward gravity. Magnetometer calibration requires rotating the unit 360° around each axis to map out magnetic field offsets (hard-iron calibration). Signal K server handles calibration offsets once configured.

### Derived Insights
- **Water current:** GPS SOG/COG minus hull speed/heading through water. Significant factor in Great Lakes racing; no consumer app currently accounts for it in routing. With GLCFS modeled currents (v1) as a baseline, measured currents (v2) provide ground truth and local refinement.
- **Learned polars:** Boat speed + true wind angle/speed + heel angle over many races → neural network learns actual performance curves for that specific hull. Replaces generic class polars with dramatically more accurate vessel-specific data. Heel data is particularly valuable because it captures the real-world relationship between wind, sail trim, and speed that static polars ignore.
- **True local wind:** Onboard wind reading is more accurate for immediate routing decisions than any gridded forecast model, which averages over a large area.
- **Sail trim efficiency:** Heel angle + boat speed + wind speed together tell you whether the sails are trimmed correctly. The AI advisor can correlate heel with speed and compare against optimal performance curves to give trim recommendations.

### Hardware Stack
- Raspberry Pi 4 or Pi Zero 2W
- NMEA 0183 USB adapter (~$30) or NMEA 2000 interface hat (~$60)
- **IMU sensor board** — BerryIMU v3 (~$30) or MPU9250 (~$4–15), connected via I2C (GPIO pins 2/3)
- Signal K server (open-source boat data aggregator — widely adopted in sailing community) running on the Pi
- 4G LTE USB modem for real-time transmission
- Total hardware cost: ~$150–230 (DIY) or ~$250–330 (pre-assembled kit)

### v1 Architecture Requirement
**The SailLine backend API must include a telemetry ingestion endpoint from day 1**, even if it is not publicly documented or the hardware is not launched. Adding this later without planning creates significant architectural debt. The endpoint receives: timestamp, lat, lon, boat speed, true wind speed, true wind direction, heading, heel, pitch. Data stored in PostGIS alongside GPS tracks.

### Business Model (v2+)
- Hardware kit sold at ~$299 (modest margin over BOM)
- Or: DIY documentation published free (Signal K is well-documented) — drives Hardware tier subscriptions
- Instrument-enhanced post-race analysis bundled into Hardware tier (no additional charge)

---

## 12. Technical Stack

| Layer | Technology |
|---|---|
| Frontend | React + MapboxGL |
| Backend | Python (FastAPI) |
| Routing engine | Custom Python isochrone solver (ensemble-aware, current-aware) |
| Ensemble weather | NOAA GEFS GRIB2 via `cfgrib` |
| Short-term weather | NOAA HRRR GRIB2 via `cfgrib` |
| Surface currents | NOAA GLERL GLCFS NetCDF via `xarray` / `netCDF4` |
| Buoy observations | NOAA NDBC (HTTP/RSS feeds) |
| AIS | Datalastic REST API (server-side cached per zone) |
| GPS + telemetry storage | PostgreSQL + PostGIS |
| ML polars (v2) | scikit-learn or PyTorch, trained on telemetry data |
| AI tactical advisor | Claude API (Pro/Hardware, real-time) |
| AI post-race analysis | Claude API (Pro/Hardware, per-race) |
| Auth | Firebase Auth (JWT) |
| Payments | Stripe |
| Hosting | GCP (Cloud Run, Cloud SQL, Memorystore Redis, Cloud Storage) |

---

## 13. Roadmap

| Release | Scope |
|---|---|
| **v1.0** | Pre-race planning · In-race routing (inshore + distance) · AI tactical advisor · AIS competitors · GLCFS modeled currents · NDBC forecast validation · NWS marine alerts · Handicap support · GPS track recording · Telemetry API endpoint (internal, not public) · Desktop UI |
| **v1.5** | Probabilistic ensemble routing (GEFS) · GLSEA surface temperature / lake breeze warnings · Wave Watch III · Tablet/cockpit responsive layout · Community mark library |
| **v2.0** | Post-race AI analysis (GPS + instruments) · Pi hardware module launch · IMU heel/pitch/trim · Telemetry ingestion goes public · Measured current detection · Instrument-enhanced post-race analysis |
| **v3.0** | ML-learned polars (per-boat, trained on instrument + heel data) · Custom polar uploads · Expanded boat classes |

---

## 14. Success Metrics

| Metric | Target |
|---|---|
| Break-even | 22 Pro subscribers |
| Free → Pro conversion | ≥ 5% |
| 90-day Pro retention | ≥ 60% |
| Inshore recalculation latency | < 3 min |
| Distance recalculation latency | < 15 min |
| AIS refresh lag | ≤ 5 min |
| Net margin at 100 Pro subscribers | ≥ +$900/mo |

---

## 15. Out of Scope — v1

- Post-race analysis UI (recording in scope; analysis UI is v2)
- Hardware module / telemetry API (planned in architecture; not public until v2)
- ML-learned polars (v3, requires accumulated telemetry data)
- Native iOS/Android apps
- Custom polar file uploads
- Ocean/offshore passage routing
- YB Tracking integration
- Community venue mark library
- Multi-user/coach/team fleet view
- Racing Rules of Sailing (RRS) adjudication (AIS refresh too slow for real-time right-of-way)
