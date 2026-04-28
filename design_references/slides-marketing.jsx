/* Cover, navigation, and marketing wireframes */

const SlideCover = () => (
    <div className="full" style={{ padding: 80, position: 'relative' }}>
        <div className="slide-meta">
            <div className="lhs">
                <span className="num">00</span><span>—</span><span className="title">COVER</span>
            </div>
            <div className="rhs">SAILLINE · WIREFRAME EXPLORATION · v1 · 2026.04</div>
        </div>

        <div style={{ position: 'absolute', left: 80, top: 220 }}>
            <div className="cover-sub" style={{ marginBottom: 32 }}>STRUCTURED WIREFRAMES · GRAY-SCALE · LO-FI</div>
            <div className="cover-title">SailLine.</div>
            <div className="cover-title" style={{ color: 'var(--ink-3)' }}>Wireframes.</div>
            <div style={{ marginTop: 48, maxWidth: 920 }}>
                <p style={{ fontSize: 22, lineHeight: 1.55, color: 'var(--ink-2)', margin: 0 }}>
                    Real-time race routing for the Great Lakes. 21 forecasts, one route — explored across the marketing site, onboarding, pre-race planning, in-race dashboard, post-race analysis, settings, and cockpit views.
                </p>
            </div>
        </div>

        {/* Sketchy compass */}
        <svg style={{ position: 'absolute', right: 120, top: 160, opacity: 0.85 }} width="380" height="380" viewBox="0 0 380 380">
            <circle cx="190" cy="190" r="170" fill="none" stroke="var(--ink)" strokeWidth="1.5" strokeDasharray="3 5" />
            <circle cx="190" cy="190" r="120" fill="none" stroke="var(--ink-3)" strokeWidth="1" />
            <circle cx="190" cy="190" r="60" fill="none" stroke="var(--ink-3)" strokeWidth="1" strokeDasharray="2 4" />
            <line x1="190" y1="20" x2="190" y2="360" stroke="var(--ink)" strokeWidth="1" />
            <line x1="20" y1="190" x2="360" y2="190" stroke="var(--ink)" strokeWidth="1" />
            <line x1="65" y1="65" x2="315" y2="315" stroke="var(--ink-3)" strokeWidth="0.8" />
            <line x1="315" y1="65" x2="65" y2="315" stroke="var(--ink-3)" strokeWidth="0.8" />
            <text x="190" y="14" textAnchor="middle" fontFamily="JetBrains Mono" fontSize="14" fontWeight="700">N</text>
            <text x="190" y="378" textAnchor="middle" fontFamily="JetBrains Mono" fontSize="14" fontWeight="700">S</text>
            <text x="370" y="195" textAnchor="middle" fontFamily="JetBrains Mono" fontSize="14" fontWeight="700">E</text>
            <text x="10" y="195" textAnchor="middle" fontFamily="JetBrains Mono" fontSize="14" fontWeight="700">W</text>
            <polygon points="190,80 200,200 190,210 180,200" fill="var(--accent)" />
        </svg>

        <div className="slide-foot">
            <div>SAILLINE · CONFIDENTIAL</div>
            <div>USE ← / → · OR PRESS S TO TOGGLE TWEAKS</div>
        </div>
    </div>
);

// MARKETING — variant A: Bloomberg-dense modular grid
const MktA = () => (
    <div className="rel" style={{ padding: '0 24px' }}>
        {/* nav */}
        <div className="between" style={{ height: 56, borderBottom: '1.5px solid var(--ink)', paddingTop: 8 }}>
            <div className="middle gap-32">
                <div className="display" style={{ fontSize: 22 }}>SailLine<span style={{ color: 'var(--accent)' }}>.</span></div>
                <Lbl>Routing · Hardware · Pricing · Docs · Race Log</Lbl>
            </div>
            <div className="middle gap-12">
                <Lbl>Sign in</Lbl>
                <Btn solid sm>Start free</Btn>
            </div>
        </div>

        {/* hero — 12-col grid, dense */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(12, 1fr)', gap: 16, marginTop: 24 }}>
            <div style={{ gridColumn: 'span 7' }}>
                <Lbl>HERO · v1.0 PRE-LAUNCH</Lbl>
                <h1 style={{ fontSize: 92, lineHeight: 0.95, letterSpacing: '-0.04em', margin: '8px 0 0', fontWeight: 700 }}>
                    21 forecasts.<br />One optimal route.<br /><span style={{ color: 'var(--ink-3)' }}>Updated every two minutes.</span>
                </h1>
                <p style={{ fontSize: 18, color: 'var(--ink-2)', maxWidth: 580, marginTop: 24 }}>
                    Probabilistic ensemble routing, ML-enhanced polars, and an AI tactical advisor — built for Great Lakes race crews.
                </p>
                <div className="middle gap-12" style={{ marginTop: 28 }}>
                    <Btn solid lg>Start free →</Btn>
                    <Btn lg>See live demo</Btn>
                </div>

                {/* live data ticker */}
                <Box style={{ marginTop: 36, padding: 14 }} thin>
                    <div className="between">
                        <Chip dot live>LIVE · MAC FLEET</Chip>
                        <Mono style={{ fontSize: 11, color: 'var(--ink-4)' }}>UPDATED 14s AGO</Mono>
                    </div>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, marginTop: 12 }}>
                        <Datum label="Wind" value="14.2 KTS" mono />
                        <Datum label="Dir" value="247°" mono />
                        <Datum label="TWA" value="38°" mono />
                        <Datum label="VMG" value="6.8 KTS" mono />
                        <Datum label="ETA" value="04:12:08" mono />
                    </div>
                </Box>
            </div>

            <div style={{ gridColumn: 'span 5', position: 'relative' }}>
                <Box label="LIVE ADVISOR DEMO · ABOVE THE FOLD" style={{ height: 460 }}>
                    <MapPlaceholder style={{ position: 'absolute', inset: 0, borderRadius: 3 }}>
                        <Isochrones cx={520} cy={420} />
                        <RouteLine d="M 200 700 L 380 520 L 620 360 L 880 200" />
                        <RouteLine d="M 200 700 L 360 540 L 600 380 L 880 200" dash color="var(--ink-3)" />
                        {[[200, 700], [620, 360], [880, 200]].map(([x, y], i) => (
                            <svg key={i} style={{ position: 'absolute', left: 0, top: 0, overflow: 'visible' }} width="100%" height="100%" viewBox="0 0 1200 800" preserveAspectRatio="none">
                                <circle cx={x} cy={y} r="9" fill="white" stroke="var(--ink)" strokeWidth="1.8" />
                                <text x={x} y={y + 4} textAnchor="middle" fontFamily="JetBrains Mono" fontSize="10" fontWeight="700">{i === 0 ? 'S' : i === 2 ? 'F' : 1}</text>
                            </svg>
                        ))}
                    </MapPlaceholder>
                    {/* floating advisor card */}
                    <div style={{ position: 'absolute', left: 16, bottom: 16, right: 16, background: 'var(--paper)', border: '1.5px solid var(--ink)', padding: 12 }}>
                        <div className="middle gap-8" style={{ marginBottom: 6 }}>
                            <Chip live>AI ADVISOR · 18 MIN</Chip>
                            <Lbl>RECOMMEND</Lbl>
                        </div>
                        <div style={{ fontSize: 15, fontWeight: 600 }}>
                            "Wind shifts 12° left in 18 min. Tack now to be on the lifted tack at the layline."
                        </div>
                    </div>
                </Box>
            </div>
        </div>

        {/* three-layer differentiator */}
        <div style={{ marginTop: 16, display: 'grid', gridTemplateColumns: 'repeat(12, 1fr)', gap: 12 }}>
            <div style={{ gridColumn: 'span 12' }}>
                <Lbl>02 · THREE-LAYER DIFFERENTIATOR</Lbl>
            </div>
            {[
                { n: '01', t: 'Probabilistic Ensemble', d: 'Isochrones across 21 NOAA GEFS members. Confidence-banded.' },
                { n: '02', t: 'ML-Enhanced Polars', d: 'Neural-net curves trained on real GPS + instrument data.' },
                { n: '03', t: 'AI Tactical Advisor', d: 'Plain-language guidance from routing math. "Tack now."' }
            ].map((l, i) => (
                <Box key={i} style={{ gridColumn: 'span 4', padding: 14, height: 130 }}>
                    <Mono style={{ fontSize: 22, color: 'var(--ink-4)' }}>{l.n}</Mono>
                    <h3 style={{ fontSize: 20, margin: '6px 0 4px', letterSpacing: '-0.02em' }}>{l.t}</h3>
                    <p style={{ fontSize: 12, color: 'var(--ink-3)', margin: 0 }}>{l.d}</p>
                </Box>
            ))}
        </div>

        {/* tier strip */}
        <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, paddingBottom: 100 }}>
            {[
                { t: 'FREE', p: '$0', l: ['Pre-race · 24hr weather'] },
                { t: 'PRO', p: '$15/mo', l: ['In-race · AIS · 7-day · advisor'], hi: true },
                { t: 'HARDWARE', p: '$25/mo', l: ['Pi telemetry · current · ML polars'] }
            ].map((tt, i) => (
                <Box key={i} style={{ padding: 12, borderColor: tt.hi ? 'var(--ink)' : 'var(--rule-2)', borderWidth: tt.hi ? 2 : 1 }}>
                    <div className="between" style={{ marginBottom: 4 }}>
                        <Mono style={{ fontWeight: 700, fontSize: 12 }}>{tt.t}</Mono>
                        <Mono style={{ fontSize: 12 }}>{tt.p}</Mono>
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--ink-3)' }}>
                        {tt.l.map((x, j) => <div key={j}>· {x}</div>)}
                    </div>
                </Box>
            ))}
        </div>

        {/* annotations */}
        <Callout x={1180} y={100} w={220}><Note>live data widget<br />above the fold ✓</Note></Callout>
    </div>
);

// MARKETING — variant B: Stripe / Linear product clean
const MktB = () => (
    <div className="rel" style={{ padding: '0 60px' }}>
        <div className="between" style={{ height: 64, paddingTop: 16 }}>
            <div className="display" style={{ fontSize: 22 }}>SailLine<span style={{ color: 'var(--accent)' }}>.</span></div>
            <div className="middle gap-24"><Lbl>Routing</Lbl><Lbl>Hardware</Lbl><Lbl>Pricing</Lbl><Lbl>Docs</Lbl><Btn sm>Sign in</Btn><Btn solid sm>Start free</Btn></div>
        </div>

        {/* big centered hero */}
        <div style={{ textAlign: 'center', marginTop: 60 }}>
            <Chip dot style={{ marginBottom: 24 }}>v1.0 · GREAT LAKES · NOW IN OPEN BETA</Chip>
            <h1 style={{ fontSize: 120, lineHeight: 0.95, letterSpacing: '-0.04em', margin: 0, fontWeight: 700, maxWidth: 1500, marginInline: 'auto' }}>
                Race intelligence,<br />not a marine app.
            </h1>
            <p style={{ fontSize: 22, color: 'var(--ink-3)', maxWidth: 760, margin: '32px auto 0' }}>
                SailLine routes you across <Mono>21 forecast scenarios</Mono> at once — then translates the math into plain-language tactics, every two minutes.
            </p>
            <div className="middle gap-12" style={{ justifyContent: 'center', marginTop: 40 }}>
                <Btn solid lg>Try it free →</Btn>
                <Btn lg>See dashboard</Btn>
            </div>
        </div>

        {/* hero artifact: dashboard preview floating */}
        <Box style={{ marginTop: 60, height: 460, padding: 0, position: 'relative' }}>
            <MapPlaceholder style={{ position: 'absolute', inset: 0 }}>
                <Isochrones cx={900} cy={300} />
                <RouteLine d="M 100 600 L 320 480 L 600 360 L 900 240 L 1100 160" />
                <RouteLine d="M 100 600 L 300 500 L 580 380 L 880 280 L 1100 160" dash color="var(--ink-3)" />
                {[0, 1, 2, 3].map(i => <WindBarb key={i} x={140 + i * 250} y={140} dir={210 + i * 5} />)}
            </MapPlaceholder>
            <Box style={{ position: 'absolute', top: 20, left: 20, padding: 14, width: 320 }}>
                <Lbl style={{ marginBottom: 8 }}>AI ADVISOR · NOW</Lbl>
                <div style={{ fontSize: 15, fontWeight: 600, lineHeight: 1.35 }}>
                    "Three ensemble models show pressure mid-lake. The conservative route hugs the Michigan shore — recommended."
                </div>
            </Box>
            <Box style={{ position: 'absolute', top: 20, right: 20, padding: 14, width: 240 }}>
                <Lbl style={{ marginBottom: 8 }}>LIVE TELEMETRY</Lbl>
                <Datum label="WIND" value="14.2 KT" />
                <Datum label="DIR" value="247°" />
                <Datum label="TWA" value="38°" />
                <Datum label="VMG" value="6.8 KT" />
                <Datum label="ETA" value="04:12" />
            </Box>
        </Box>

        <Callout x={780} y={140} w={280} blue><Note>"Race intelligence" tagline<br />front and center —<br />positions vs lifestyle apps</Note></Callout>
    </div>
);

// MARKETING — variant C: SailGP/F1 cinematic + Apple hardware drop
const MktC = () => (
    <div className="full rel">
        {/* sticky nav */}
        <div className="between" style={{ height: 56, padding: '12px 36px', borderBottom: '1px solid var(--rule)' }}>
            <div className="display" style={{ fontSize: 22 }}>SailLine<span style={{ color: 'var(--accent)' }}>.</span></div>
            <Mono style={{ fontSize: 11, color: 'var(--ink-3)' }}>EST. 04:12 · 21 SCENARIOS · LIVE</Mono>
        </div>

        {/* full-bleed hero with overlaid copy */}
        <div style={{ position: 'absolute', top: 56, left: 0, right: 0, bottom: 80, background: 'var(--ink)', overflow: 'hidden' }}>
            {/* sketchy "race photo" placeholder */}
            <div className="ph" style={{ position: 'absolute', inset: 0, opacity: 0.18 }} />
            <svg style={{ position: 'absolute', inset: 0 }} width="100%" height="100%" preserveAspectRatio="none" viewBox="0 0 1840 800">
                <text x="60" y="100" fontFamily="JetBrains Mono" fontSize="13" fill="rgba(255,255,255,0.4)">[ DRAMATIC RACE PHOTO · FULL-BLEED · HEEL/SPRAY/FOILING ]</text>
                {/* wind streamlines */}
                <g stroke="rgba(255,255,255,0.25)" fill="none" strokeWidth="0.8">
                    {Array.from({ length: 24 }, (_, i) => (
                        <path key={i} d={`M -50 ${40 + i * 32} Q 600 ${30 + i * 32} 1900 ${50 + i * 32}`} />
                    ))}
                </g>
            </svg>
            <div style={{ position: 'absolute', left: 60, bottom: 60, color: 'var(--paper)' }}>
                <Mono style={{ fontSize: 11, color: 'rgba(255,255,255,0.6)', letterSpacing: '0.2em' }}>RACE INTELLIGENCE · GREAT LAKES</Mono>
                <h1 style={{ fontSize: 120, lineHeight: 0.95, letterSpacing: '-0.04em', margin: '12px 0 0', fontWeight: 700 }}>
                    21 forecasts.<br />One optimal<br />route.
                </h1>
                <div className="middle gap-12" style={{ marginTop: 32 }}>
                    <Btn solid lg style={{ background: 'var(--paper)', color: 'var(--ink)' }}>Start free →</Btn>
                    <Btn lg style={{ borderColor: 'rgba(255,255,255,0.6)', color: 'var(--paper)' }}>See the math</Btn>
                </div>
            </div>
            <div style={{ position: 'absolute', right: 60, bottom: 60, textAlign: 'right', color: 'var(--paper)' }}>
                <Mono style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)' }}>WIND · 14.2 KT · 247°</Mono><br />
                <Mono style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)' }}>TWA · 38° · VMG · 6.8 KT</Mono>
            </div>
        </div>

        <Callout x={1380} y={100} w={300}><Note>cinematic full-bleed hero —<br />SailGP / F1 energy.<br />video or wind-streamline motion ↑</Note></Callout>
    </div>
);

// HARDWARE SECTION — Apple-style product launch
const SlideHardware = () => (
    <SlideShell num="MKT-04" title="Marketing · Hardware module · Apple-style product launch">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 60, height: '100%' }}>
            <div className="rel" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <div className="rel" style={{ width: 480, height: 480 }}>
                    {/* product on pedestal */}
                    <div className="ph-x" style={{ position: 'absolute', inset: 0, opacity: 0.4 }} />
                    <Box style={{ position: 'absolute', inset: '20% 20%', padding: 0 }}>
                        <div className="full center"><Mono style={{ fontSize: 11, color: 'var(--ink-4)' }}>[ PI MODULE · HERO RENDER ]</Mono></div>
                    </Box>
                </div>
                <Callout x={20} y={40} w={260}><Note>centered product shot,<br />generous negative space —<br />"Apple keynote moment"</Note></Callout>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
                <Lbl>05 · NEW · OPTIONAL HARDWARE</Lbl>
                <h1 style={{ fontSize: 96, lineHeight: 0.95, letterSpacing: '-0.04em', margin: '12px 0 0', fontWeight: 700 }}>
                    Your boat,<br />online.
                </h1>
                <p style={{ fontSize: 20, color: 'var(--ink-3)', marginTop: 24, maxWidth: 560 }}>
                    The SailLine Pi module plugs into NMEA 0183 / 2000 and streams real wind, boat-speed, heading, and heel — turning your existing instruments into a live racing data lake.
                </p>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16, marginTop: 36 }}>
                    <Datum label="Hardware" value="$200 DIY" big />
                    <Datum label="Subscription" value="$25/mo" big />
                    <Datum label="Setup" value="< 1 hr" big />
                </div>
                <div className="middle gap-12" style={{ marginTop: 36 }}>
                    <Btn solid lg>Pre-order kit →</Btn>
                    <Btn lg>DIY guide</Btn>
                </div>
            </div>
        </div>
    </SlideShell>
);

Object.assign(window, { SlideCover, MktA, MktB, MktC, SlideHardware });
