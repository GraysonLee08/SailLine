/* Post-race analysis, settings, mobile/cockpit */

const PostA = () => (
    <SlideShell num="POST-01" title="Post-race · Variant A · Replay timeline split">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 380px', gap: 16, height: '100%' }}>
            <div style={{ display: 'grid', gridTemplateRows: '1fr 200px', gap: 12 }}>
                <Box label="REPLAY · ACTUAL vs OPTIMAL" style={{ padding: 0, position: 'relative' }}>
                    <MapPlaceholder style={{ position: 'absolute', inset: 0 }}>
                        <RouteLine d="M 220 700 L 380 540 L 660 400 L 940 260 L 1140 160" color="var(--ink)" />
                        <RouteLine d="M 220 700 L 420 580 L 580 540 L 720 460 L 880 360 L 1020 280 L 1140 160" dash color="var(--accent)" w={2} />
                        {/* tack markers */}
                        {[[420, 580], [720, 460], [1020, 280]].map(([x, y], i) => (
                            <svg key={i} style={{ position: 'absolute', inset: 0, overflow: 'visible' }} width="100%" height="100%" viewBox="0 0 1200 800" preserveAspectRatio="none">
                                <circle cx={x} cy={y} r="6" fill="var(--accent)" />
                                <text x={x + 10} y={y + 4} fontFamily="JetBrains Mono" fontSize="10" fill="var(--accent)">T{i + 1}</text>
                            </svg>
                        ))}
                    </MapPlaceholder>
                    <Box thin style={{ position: 'absolute', top: 14, left: 14, padding: 10 }}>
                        <Lbl>LEGEND</Lbl>
                        <div style={{ fontSize: 11, marginTop: 4 }}><Mono>—— actual GPS</Mono> · <Mono style={{ color: 'var(--accent)' }}>- - - re-simulated optimal</Mono></div>
                    </Box>
                </Box>
                <Box label="TIMELINE · SPEED vs OPTIMAL · TACKS · WIND SHIFTS" style={{ padding: 14 }}>
                    <svg width="100%" height="100%" viewBox="0 0 1200 140">
                        <path d="M 0 70 Q 80 40 160 60 T 320 50 T 480 70 T 640 80 T 800 50 T 960 60 T 1180 40" stroke="var(--ink)" strokeWidth="1.5" fill="none" />
                        <path d="M 0 60 Q 80 35 160 50 T 320 40 T 480 55 T 640 60 T 800 45 T 960 50 T 1180 35" stroke="var(--accent)" strokeDasharray="4 3" strokeWidth="1.5" fill="none" />
                        {[180, 420, 720, 980].map((x, i) => (<g key={i}><line x1={x} y1="0" x2={x} y2="140" stroke="var(--ink-3)" strokeDasharray="2 3" /><text x={x + 4} y="14" fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">{['T1', 'T2', 'SHIFT', 'T3'][i]}</text></g>))}
                        <text x="0" y="135" fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">13:00</text>
                        <text x="600" y="135" fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">15:30</text>
                        <text x="1140" y="135" fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">17:45</text>
                    </svg>
                    {/* scrubber */}
                    <div className="middle gap-8" style={{ marginTop: 8 }}>
                        <Btn sm>⏮</Btn><Btn sm solid>▶</Btn><Btn sm>⏭</Btn>
                        <Mono style={{ fontSize: 11, color: 'var(--ink-3)' }}>14:32:08 · 1.0×</Mono>
                        <div style={{ flex: 1, height: 6, background: 'var(--rule)', position: 'relative' }}>
                            <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: '42%', background: 'var(--ink)' }} />
                        </div>
                    </div>
                </Box>
            </div>
            <div className="flex col gap-12">
                <Box label="AI SUMMARY" style={{ padding: 14 }}>
                    <Mono style={{ fontWeight: 700, fontSize: 13 }}>+04:36 vs OPTIMAL</Mono>
                    <Lbl style={{ marginTop: 4 }}>3 RANKED IMPROVEMENTS</Lbl>
                    <ol style={{ margin: '10px 0 0', padding: '0 0 0 18px', fontSize: 13, lineHeight: 1.45 }}>
                        <li>Missed left shift at 14:18 — held starboard 3.5 min too long. <Mono style={{ color: 'var(--accent)' }}>−2:12</Mono></li>
                        <li>Overstood layline at Mark 2 by ~120 m. <Mono style={{ color: 'var(--accent)' }}>−1:08</Mono></li>
                        <li>VMG 4% below polar through middle of Leg 3. <Mono style={{ color: 'var(--accent)' }}>−1:16</Mono></li>
                    </ol>
                </Box>
                <Box label="KEY METRICS" style={{ padding: 14 }}>
                    <Datum label="ELAPSED" value="04:18:32" />
                    <Datum label="CORRECTED" value="04:08:14" />
                    <Datum label="POSITION" value="3 / 11" />
                    <Datum label="AVG VMG" value="6.42 KT" />
                    <Datum label="TACKS" value="7 (3 unforced)" />
                </Box>
                <Box label="DECISION POINTS" style={{ padding: 14, flex: 1 }}>
                    {['T1 14:08 · ✓ correct', 'T2 14:42 · ⚠ late by 3.5 min', 'SHIFT 15:14 · ✗ missed', 'T3 16:02 · ✓ correct'].map((d, i) => (
                        <div key={i} style={{ padding: '8px 0', borderBottom: i < 3 ? '1px dashed var(--hair)' : 'none', fontSize: 11, fontFamily: 'JetBrains Mono' }}>{d}</div>
                    ))}
                </Box>
            </div>
        </div>
    </SlideShell>
);

const PostB = () => (
    <SlideShell num="POST-02" title="Post-race · Variant B · Narrative AI report">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 32, height: '100%' }}>
            <div>
                <Lbl>RACE 14 · MORF · WED 14 MAY 2026</Lbl>
                <h1 style={{ fontSize: 64, lineHeight: 1, letterSpacing: '-0.03em', margin: '8px 0 24px', fontWeight: 700 }}>
                    You finished 3rd.<br />You should have finished 1st.
                </h1>
                <p style={{ fontSize: 17, color: 'var(--ink-2)', lineHeight: 1.5, maxWidth: 600 }}>
                    On a 14-knot southwesterly that left-shifted twice, your start and first beat were strong — you crossed the windward gate in second. The decisive loss was a 3.5-minute hesitation on the second left shift at 14:42, where 17 of 21 ensemble members had agreed since 13:50. That single tack cost <Mono style={{ background: 'var(--highlight)', padding: '0 4px' }}>2:12</Mono>.
                </p>
                <p style={{ fontSize: 17, color: 'var(--ink-2)', lineHeight: 1.5, maxWidth: 600 }}>
                    Two smaller losses compounded: a 120m overstand at Mark 2 (<Mono>−1:08</Mono>) and a 4% VMG deficit through the middle of Leg 3, likely sail trim (<Mono>−1:16</Mono>). Net: <Mono style={{ background: 'var(--highlight)', padding: '0 4px' }}>+04:36 vs the optimal route</Mono>.
                </p>
                <div className="middle gap-12" style={{ marginTop: 32 }}>
                    <Btn solid>Open replay →</Btn>
                    <Btn>Export PDF</Btn>
                    <Btn ghost>Share with crew</Btn>
                </div>
            </div>
            <div className="flex col gap-12">
                <Box label="THE 3 MOMENTS THAT MATTERED" style={{ padding: 0 }}>
                    {[
                        { t: '14:42 · LEG 2', d: 'Held starboard through agreed left shift', l: '−2:12' },
                        { t: '15:48 · MARK 2', d: 'Overstood layline by ~120 m', l: '−1:08' },
                        { t: '16:14 · LEG 3', d: '4% below polar VMG · likely main twist', l: '−1:16' }
                    ].map((m, i) => (
                        <div key={i} className="between" style={{ padding: '16px 18px', borderBottom: i < 2 ? '1px solid var(--rule)' : 'none' }}>
                            <div>
                                <Mono style={{ fontSize: 11, color: 'var(--ink-4)' }}>{m.t}</Mono>
                                <div style={{ fontSize: 14, marginTop: 4 }}>{m.d}</div>
                            </div>
                            <Mono style={{ fontSize: 22, fontWeight: 700, color: 'var(--accent)' }}>{m.l}</Mono>
                        </div>
                    ))}
                </Box>
                <Box label="REPLAY THUMB" style={{ padding: 0, height: 240, position: 'relative' }}>
                    <MapPlaceholder style={{ position: 'absolute', inset: 0 }}>
                        <RouteLine d="M 200 600 L 380 480 L 660 360 L 940 240" />
                        <RouteLine d="M 200 600 L 420 540 L 720 460 L 940 240" dash color="var(--accent)" w={2} />
                    </MapPlaceholder>
                </Box>
            </div>
        </div>
    </SlideShell>
);

const PostC = () => (
    <SlideShell num="POST-03" title="Post-race · Variant C · Race log dashboard (career view)">
        <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: 16, height: '100%' }}>
            <Box label="RACE LOG" style={{ padding: 0 }}>
                {[
                    { d: '14 MAY', n: 'MORF #14', r: '3 / 11', s: '+04:36' },
                    { d: '07 MAY', n: 'MORF #13', r: '5 / 12', s: '+07:12', sel: true },
                    { d: '30 APR', n: 'TUNE-UP', r: '—', s: 'practice' },
                    { d: '27 APR', n: 'MORF #12', r: '2 / 10', s: '+02:08' },
                    { d: '20 APR', n: 'MORF #11', r: '6 / 11', s: '+09:50' }
                ].map((rc, i) => (
                    <div key={i} className="between" style={{ padding: '12px 14px', borderBottom: '1px dashed var(--hair)', background: rc.sel ? 'var(--paper-2)' : 'transparent' }}>
                        <div>
                            <Mono style={{ fontSize: 10, color: 'var(--ink-4)' }}>{rc.d}</Mono>
                            <Mono style={{ fontWeight: 700, fontSize: 12, display: 'block' }}>{rc.n}</Mono>
                        </div>
                        <div style={{ textAlign: 'right' }}>
                            <Mono style={{ fontSize: 11 }}>{rc.r}</Mono>
                            <Mono style={{ fontSize: 10, color: 'var(--accent)', display: 'block' }}>{rc.s}</Mono>
                        </div>
                    </div>
                ))}
            </Box>
            <div style={{ display: 'grid', gridTemplateRows: 'auto 1fr', gap: 12 }}>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
                    {[['SEASON AVG', '+05:48 vs opt'], ['VMG TREND', '+3.2% over 30d'], ['TACKING', '82% on shift'], ['FINISH AVG', '3.8 / fleet']].map(([l, v], i) => (
                        <Box key={i} style={{ padding: 14 }}>
                            <Lbl>{l}</Lbl>
                            <Mono style={{ fontSize: 24, fontWeight: 700, marginTop: 6, display: 'block' }}>{v}</Mono>
                        </Box>
                    ))}
                </div>
                <Box label="SEASON · TIME LOST PER RACE" style={{ padding: 14 }}>
                    <svg width="100%" height="100%" viewBox="0 0 1100 280">
                        {[3.6, 5.1, 8.2, 2.1, 6.4, 4.5, 9.8, 3.8, 7.1, 4.6].map((v, i) => (
                            <g key={i}>
                                <rect x={60 + i * 100} y={240 - v * 22} width="50" height={v * 22} fill="var(--ink)" />
                                <text x={85 + i * 100} y="260" textAnchor="middle" fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">R{i + 1}</text>
                            </g>
                        ))}
                        <line x1="40" y1="160" x2="1080" y2="160" stroke="var(--accent)" strokeDasharray="3 4" />
                        <text x="1085" y="164" fontFamily="JetBrains Mono" fontSize="10" fill="var(--accent)">avg</text>
                    </svg>
                </Box>
            </div>
        </div>
    </SlideShell>
);

// SETTINGS
const SetA = () => (
    <SlideShell num="SET-01" title="Settings · Boat profile · Variant A (form)">
        <div style={{ display: 'grid', gridTemplateColumns: '240px 1fr', gap: 24, height: '100%' }}>
            <div className="flex col gap-4">
                {['Account', 'Boat profile', 'Polars', 'Handicap', 'Telemetry (Pi)', 'Notifications', 'Billing', 'Privacy & data'].map((x, i) => (
                    <div key={i} style={{ padding: '10px 14px', borderRadius: 4, background: i === 1 ? 'var(--ink)' : 'transparent', color: i === 1 ? 'var(--paper)' : 'var(--ink-2)' }}>
                        <Mono style={{ fontSize: 12 }}>{x}</Mono>
                    </div>
                ))}
            </div>
            <div>
                <Lbl>BOAT PROFILE</Lbl>
                <h2 style={{ fontSize: 36, margin: '4px 0 24px', letterSpacing: '-0.02em' }}>Halyard <span style={{ color: 'var(--ink-3)' }}>· Beneteau First 36.7</span></h2>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
                    {[
                        ['BOAT NAME', 'Halyard'], ['SAIL #', 'USA 14422'],
                        ['CLASS', 'Beneteau First 36.7'], ['LOA', '11.0 m'],
                        ['HOME WATERS', 'Lake Michigan · Monroe Stn'], ['HANDICAP', 'PHRF · 78'],
                        ['CREW WEIGHT', '690 kg'], ['BOTTOM', 'Cleaned 2026-04-12']
                    ].map(([l, v], i) => (
                        <div key={i}>
                            <Lbl style={{ marginBottom: 6 }}>{l}</Lbl>
                            <div style={{ height: 42, border: '1.5px solid var(--ink)', borderRadius: 4, padding: '0 12px', display: 'flex', alignItems: 'center' }}><Mono>{v}</Mono></div>
                        </div>
                    ))}
                </div>
                <div style={{ marginTop: 32 }}>
                    <Lbl style={{ marginBottom: 8 }}>HANDICAP SYSTEMS · CHOOSE ALL THAT APPLY</Lbl>
                    <div className="middle gap-8">
                        {['PHRF', 'ORC', 'ORR-EZ', 'IRC', 'MORF'].map((h, i) => (
                            <Chip key={i} solid={i === 0 || i === 4} dot>{h}</Chip>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    </SlideShell>
);

const SetB = () => (
    <SlideShell num="SET-02" title="Settings · Telemetry / Pi module · Variant B (status console)">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, height: '100%' }}>
            <div>
                <Lbl>HARDWARE TIER</Lbl>
                <h2 style={{ fontSize: 36, margin: '4px 0 24px', letterSpacing: '-0.02em' }}>Pi telemetry status</h2>
                <Box style={{ padding: 18 }}>
                    <div className="between"><Chip live dot>CONNECTED</Chip><Mono style={{ fontSize: 11, color: 'var(--ink-4)' }}>last packet 2 s ago</Mono></div>
                    <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                        <Datum label="DEVICE ID" value="SL-PI-0042" />
                        <Datum label="FIRMWARE" value="v2.1.4" />
                        <Datum label="PROTOCOL" value="NMEA 2000" />
                        <Datum label="SIGNAL K" value="v2.3.1" />
                        <Datum label="UPLINK" value="4G LTE · −68 dBm" />
                        <Datum label="UPTIME" value="14d 06:22" />
                    </div>
                </Box>
                <Box style={{ padding: 18, marginTop: 16 }}>
                    <Lbl style={{ marginBottom: 10 }}>ACTIVE SENTENCES</Lbl>
                    {[['$GPRMC', 'GPS position', '✓'], ['$IIVHW', 'Boat speed thru water', '✓'], ['$IIMWD', 'True wind', '✓'], ['$IIMWV', 'Apparent wind', '✓'], ['$IIHDG', 'Magnetic heading', '✓'], ['IMU', 'Heel angle', '— derived']].map((r, i) => (
                        <div key={i} className="between" style={{ padding: '8px 0', borderBottom: i < 5 ? '1px dashed var(--hair)' : 'none' }}>
                            <Mono style={{ fontSize: 12 }}>{r[0]} <span style={{ color: 'var(--ink-4)' }}>· {r[1]}</span></Mono>
                            <Mono style={{ fontSize: 11, color: r[2] === '✓' ? 'var(--accent)' : 'var(--ink-4)' }}>{r[2]}</Mono>
                        </div>
                    ))}
                </Box>
            </div>
            <div className="flex col gap-12">
                <Box label="LIVE STREAM · LAST 60 S" style={{ padding: 14, flex: 1 }}>
                    <svg width="100%" height="100%" viewBox="0 0 600 380">
                        {['BOAT SPEED', 'TRUE WIND', 'HEADING', 'HEEL'].map((label, k) => (
                            <g key={k}>
                                <text x="0" y={20 + k * 90} fontFamily="JetBrains Mono" fontSize="10" fill="var(--ink-4)">{label}</text>
                                <path d={`M 0 ${50 + k * 90} ${Array.from({ length: 30 }, (_, i) => `L ${i * 20} ${50 + k * 90 + Math.sin(i * 0.4 + k) * 12 + Math.cos(i * 0.7 + k) * 8}`).join(' ')}`} stroke="var(--ink)" strokeWidth="1.2" fill="none" />
                            </g>
                        ))}
                    </svg>
                </Box>
                <Box label="LEARNED POLAR · DELTA vs GENERIC" style={{ padding: 14 }}>
                    <Datum label="DATA POINTS" value="14,203 (12 races)" />
                    <Datum label="MAX UPLIFT" value="+0.42 KT @ TWA 95° / 14 KT" />
                    <Datum label="STATUS" value="LEARNING · v3 in 2 races" />
                </Box>
            </div>
        </div>
    </SlideShell>
);

// MOBILE / COCKPIT — 3 variants side-by-side at 1080
const Cockpit = () => (
    <SlideShell num="COCK-01" title="Cockpit / tablet · 3 variants · 1024×768 landscape">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 32, height: '100%' }}>
            {/* Variant 1 — XL telemetry card */}
            <div className="flex col gap-12">
                <Lbl>A · GIANT NUMBERS</Lbl>
                <Box style={{ padding: 0, flex: 1, position: 'relative', background: 'var(--ink)', borderColor: 'var(--ink)' }}>
                    <div style={{ position: 'absolute', inset: 0, padding: 24, color: 'var(--paper)' }}>
                        <div className="between" style={{ marginBottom: 8 }}>
                            <Chip style={{ background: 'var(--accent)', color: 'white', borderColor: 'var(--accent)' }}>TACK NOW</Chip>
                            <Mono style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)' }}>01:12:08</Mono>
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 16 }}>
                            <div><Lbl style={{ color: 'rgba(255,255,255,0.5)' }}>TWA</Lbl><div style={{ fontSize: 72, fontWeight: 700, fontFamily: 'JetBrains Mono' }}>38°</div></div>
                            <div><Lbl style={{ color: 'rgba(255,255,255,0.5)' }}>VMG</Lbl><div style={{ fontSize: 72, fontWeight: 700, fontFamily: 'JetBrains Mono' }}>6.8</div></div>
                            <div><Lbl style={{ color: 'rgba(255,255,255,0.5)' }}>WIND</Lbl><div style={{ fontSize: 48, fontWeight: 700, fontFamily: 'JetBrains Mono' }}>14.2</div></div>
                            <div><Lbl style={{ color: 'rgba(255,255,255,0.5)' }}>HDG</Lbl><div style={{ fontSize: 48, fontWeight: 700, fontFamily: 'JetBrains Mono' }}>012°</div></div>
                        </div>
                        <div style={{ position: 'absolute', bottom: 24, left: 24, right: 24, padding: 14, border: '1.5px solid rgba(255,255,255,0.3)', borderRadius: 4 }}>
                            <Mono style={{ fontSize: 14, color: 'var(--paper)' }}>"Tack now — lifted tack at layline in 4 min."</Mono>
                        </div>
                    </div>
                </Box>
                <Lbl dk>cockpit-readable in sun</Lbl>
            </div>

            {/* Variant 2 — Map + bar */}
            <div className="flex col gap-12">
                <Lbl>B · MAP + BOTTOM BAR</Lbl>
                <Box style={{ padding: 0, flex: 1, position: 'relative' }}>
                    <MapPlaceholder style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 90 }}>
                        <RouteLine d="M 60 380 L 200 280 L 360 200 L 480 120" />
                        <svg style={{ position: 'absolute', inset: 0, overflow: 'visible' }} width="100%" height="100%" viewBox="0 0 540 480" preserveAspectRatio="none">
                            <circle cx="60" cy="380" r="10" fill="var(--accent)" stroke="var(--ink)" strokeWidth="2" />
                        </svg>
                    </MapPlaceholder>
                    <Box thin style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: 90, padding: 14, borderRadius: 0, borderTop: '1.5px solid var(--ink)', display: 'flex', alignItems: 'center', gap: 16 }}>
                        <Datum label="TWA" value="38°" big />
                        <Datum label="VMG" value="6.8" big />
                        <Datum label="WIND" value="14.2" big />
                        <div style={{ flex: 1 }} />
                        <Btn solid>TACK</Btn>
                    </Box>
                </Box>
                <Lbl dk>spatial awareness preserved</Lbl>
            </div>

            {/* Variant 3 — Stacked panels (portrait-ish) */}
            <div className="flex col gap-12">
                <Lbl>C · ADVISOR-FIRST</Lbl>
                <Box style={{ padding: 0, flex: 1, display: 'flex', flexDirection: 'column' }}>
                    <div style={{ padding: 18, background: 'var(--ink)', color: 'var(--paper)' }}>
                        <Lbl style={{ color: 'rgba(255,255,255,0.5)' }}>AI · NOW</Lbl>
                        <div style={{ fontSize: 24, fontWeight: 600, lineHeight: 1.2, marginTop: 8 }}>Tack now — wind shifts −12° L in 18 min.</div>
                        <div className="middle gap-8" style={{ marginTop: 16 }}>
                            <Btn solid sm style={{ background: 'var(--paper)', color: 'var(--ink)', borderColor: 'var(--paper)' }}>✓ Done</Btn>
                            <Btn sm style={{ borderColor: 'rgba(255,255,255,0.5)', color: 'var(--paper)' }}>Snooze</Btn>
                        </div>
                    </div>
                    <div style={{ padding: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                        {[['TWA', '38°'], ['VMG', '6.8'], ['WIND', '14.2'], ['HDG', '012°'], ['DIST', '2.4 NM'], ['ETA', '21:14']].map(([l, v], i) => (
                            <div key={i}><Lbl>{l}</Lbl><Mono style={{ fontSize: 24, fontWeight: 700 }}>{v}</Mono></div>
                        ))}
                    </div>
                    <div style={{ flex: 1, position: 'relative', borderTop: '1px solid var(--rule)' }}>
                        <MapPlaceholder style={{ position: 'absolute', inset: 0 }}>
                            <RouteLine d="M 40 180 L 200 120 L 360 60" />
                        </MapPlaceholder>
                    </div>
                </Box>
                <Lbl dk>advisor-first vertical</Lbl>
            </div>
        </div>
    </SlideShell>
);

Object.assign(window, { PostA, PostB, PostC, SetA, SetB, Cockpit });
