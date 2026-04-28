/* Pre-race planning, in-race dashboard variants */

const PreA = () => (
    <SlideShell num="PRE-01" title="Pre-race planning · Variant A · Map-dominant with right rail">
        <div className="rel" style={{ display: 'grid', gridTemplateColumns: '1fr 360px', gap: 16, height: '100%' }}>
            <div className="rel">
                <div className="between" style={{ marginBottom: 12 }}>
                    <div className="middle gap-12">
                        <Mono style={{ fontWeight: 700 }}>NEW RACE</Mono>
                        <Chip dot>DRAFT · UNSAVED</Chip>
                        <Lbl>SAT 09 MAY · 13:00 CDT · LAKE MICHIGAN</Lbl>
                    </div>
                    <div className="middle gap-8">
                        <Btn sm>Save draft</Btn>
                        <Btn sm solid>Generate route →</Btn>
                    </div>
                </div>
                <Box style={{ height: 'calc(100% - 50px)', padding: 0, position: 'relative' }}>
                    <MapPlaceholder style={{ position: 'absolute', inset: 0 }}>
                        <Isochrones cx={680} cy={400} />
                        <RouteLine d="M 220 640 L 420 460 L 700 360 L 980 240" />
                        {[[220, 640, 'S'], [420, 460, '1'], [700, 360, '2'], [980, 240, 'F']].map(([x, y, n], i) => (
                            <svg key={i} style={{ position: 'absolute', inset: 0, overflow: 'visible' }} width="100%" height="100%" viewBox="0 0 1200 800" preserveAspectRatio="none">
                                <circle cx={x} cy={y} r="11" fill="white" stroke="var(--ink)" strokeWidth="2" />
                                <text x={x} y={y + 4} textAnchor="middle" fontFamily="JetBrains Mono" fontSize="11" fontWeight="700">{n}</text>
                            </svg>
                        ))}
                        {[150, 400, 650, 900].map((x, i) => <WindBarb key={i} x={x} y={120 + (i % 2) * 40} dir={210 + i * 4} />)}
                    </MapPlaceholder>

                    {/* mark placement toolbar */}
                    <Box thin style={{ position: 'absolute', top: 16, left: 16, padding: 8, display: 'flex', gap: 8 }}>
                        <Btn sm solid>+ Mark</Btn>
                        <Btn sm>+ Start line</Btn>
                        <Btn sm>+ Finish</Btn>
                        <Btn sm ghost>↶ Undo</Btn>
                    </Box>
                    {/* legend */}
                    <Box thin style={{ position: 'absolute', bottom: 16, left: 16, padding: 12 }}>
                        <Lbl style={{ marginBottom: 6 }}>LEGEND</Lbl>
                        <Datum label="Optimal route" value="—— solid" mono={false} />
                        <Datum label="Conservative" value="- - - dash" mono={false} />
                        <Datum label="Confidence band" value="grey halo" mono={false} />
                    </Box>
                </Box>
            </div>

            {/* right rail */}
            <div className="flex col gap-12" style={{ minHeight: 0 }}>
                <Box label="COURSE" style={{ padding: 14 }}>
                    {['START · Monroe Stn 41.886 -87.609', 'MARK 1 · Pres. ledge 42.18 -87.41', 'MARK 2 · Wilmette buoy 42.07 -87.61', 'FINISH · Inside CYC 41.89 -87.60'].map((m, i) => (
                        <div key={i} className="middle gap-8" style={{ padding: '6px 0', borderBottom: i < 3 ? '1px dashed var(--hair)' : 'none' }}>
                            <div style={{ width: 18, height: 18, borderRadius: '50%', border: '1.5px solid var(--ink)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                                <Mono style={{ fontSize: 9 }}>{i === 0 ? 'S' : i === 3 ? 'F' : i}</Mono>
                            </div>
                            <Mono style={{ fontSize: 11 }}>{m}</Mono>
                        </div>
                    ))}
                </Box>
                <Box label="BOAT · POLAR" style={{ padding: 14 }}>
                    <Datum label="CLASS" value="Beneteau 36.7" />
                    <Datum label="POLAR" value="GENERIC v2.1" />
                    <Datum label="HANDICAP" value="PHRF 78" />
                </Box>
                <Box label="FORECAST · 21 GEFS MEMBERS" style={{ padding: 14, flex: 1 }}>
                    <Datum label="MEAN WIND" value="14.2 KT" big />
                    <Datum label="DIR" value="247° ± 8°" />
                    <Datum label="SHIFT @ 16:00" value="−12° L (p=0.78)" />
                    <Datum label="WAVES" value="0.6 M · 4 S" />
                    <div style={{ marginTop: 12 }}>
                        <Lbl>ENSEMBLE SPREAD</Lbl>
                        <svg width="100%" height="60" viewBox="0 0 320 60">
                            {Array.from({ length: 21 }, (_, i) => (
                                <line key={i} x1={20 + i * 14} y1={50 - Math.sin(i) * 15 - 15} x2={20 + i * 14} y2={50} stroke="var(--ink-3)" strokeWidth="1" />
                            ))}
                            <line x1="20" y1="20" x2="310" y2="20" stroke="var(--accent)" strokeWidth="1.5" strokeDasharray="3 3" />
                        </svg>
                    </div>
                </Box>
                <Box label="PREDICTION" style={{ padding: 14 }}>
                    <Datum label="ELAPSED" value="04:12:30" />
                    <Datum label="CORRECTED · PHRF" value="04:08:14" />
                    <Datum label="CONFIDENCE" value="8 / 10 MEMBERS" />
                </Box>
            </div>

            <Callout x={1180} y={300} w={240}><Note>handicap shown alongside<br />elapsed — first-class<br />per brief ✓</Note></Callout>
        </div>
    </SlideShell>
);

const PreB = () => (
    <SlideShell num="PRE-02" title="Pre-race planning · Variant B · Wizard / step-by-step">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 32, height: '100%' }}>
            <div className="flex col gap-16">
                {[
                    { n: '01', t: 'WHEN', d: 'Sat 09 May · 13:00 CDT · 4 hrs window' },
                    { n: '02', t: 'WHERE', d: 'Lake Michigan · Monroe Stn → Wilmette' },
                    { n: '03', t: 'COURSE', d: '4 marks placed · 12.6 nm rhumb', sel: true },
                    { n: '04', t: 'BOAT', d: 'Beneteau 36.7 · PHRF 78' },
                    { n: '05', t: 'PREVIEW' }
                ].map((s, i) => (
                    <Box key={i} style={{ padding: 18, borderWidth: s.sel ? 2 : 1, borderColor: s.sel ? 'var(--ink)' : 'var(--rule-2)' }}>
                        <div className="between">
                            <div className="middle gap-12">
                                <Mono style={{ color: 'var(--ink-4)', fontSize: 18 }}>{s.n}</Mono>
                                <Mono style={{ fontWeight: 700, fontSize: 14 }}>{s.t}</Mono>
                            </div>
                            {s.sel && <Chip solid>EDITING</Chip>}
                        </div>
                        {s.d && <div style={{ marginTop: 8, color: 'var(--ink-3)', fontSize: 13 }}>{s.d}</div>}
                    </Box>
                ))}
            </div>
            <Box label="STEP 03 · COURSE" style={{ padding: 0, position: 'relative' }}>
                <MapPlaceholder style={{ position: 'absolute', inset: 0 }}>
                    <RouteLine d="M 200 600 L 380 460 L 660 340 L 920 220" />
                    {[[200, 600], [380, 460], [660, 340], [920, 220]].map(([x, y], i) => (
                        <svg key={i} style={{ position: 'absolute', inset: 0, overflow: 'visible' }} width="100%" height="100%" viewBox="0 0 1200 800" preserveAspectRatio="none">
                            <circle cx={x} cy={y} r="10" fill="white" stroke="var(--ink)" strokeWidth="2" />
                            <text x={x} y={y + 4} textAnchor="middle" fontFamily="JetBrains Mono" fontSize="10" fontWeight="700">{i === 0 ? 'S' : i === 3 ? 'F' : i}</text>
                        </svg>
                    ))}
                </MapPlaceholder>
                <Box thin style={{ position: 'absolute', bottom: 16, left: 16, right: 16, padding: 12 }}>
                    <Lbl style={{ marginBottom: 6 }}>HINT</Lbl>
                    <Mono style={{ fontSize: 12 }}>Click anywhere to drop a mark · drag to reposition · ⌫ to delete</Mono>
                </Box>
            </Box>
            <Callout x={520} y={520} w={260}><Note>linear wizard hides<br />complexity — better for<br />first-time users</Note></Callout>
        </div>
    </SlideShell>
);

const PreC = () => (
    <SlideShell num="PRE-03" title="Pre-race planning · Variant C · Bloomberg-style 4-pane">
        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gridTemplateRows: '1fr 1fr', gap: 12, height: '100%' }}>
            <Box label="CHART · 1280 × 720" style={{ padding: 0, gridRow: '1 / span 2', position: 'relative' }}>
                <MapPlaceholder style={{ position: 'absolute', inset: 0 }}>
                    <Isochrones cx={680} cy={420} />
                    <RouteLine d="M 220 640 L 420 480 L 700 360 L 980 240" />
                    <RouteLine d="M 220 640 L 380 540 L 680 400 L 980 240" dash color="var(--ink-3)" />
                    {[150, 400, 650, 900, 1100].map((x, i) => <WindBarb key={i} x={x} y={100 + (i % 2) * 30} dir={210 + i * 4} />)}
                </MapPlaceholder>
            </Box>
            <Box label="ENSEMBLE FAN · 21 MEMBERS" style={{ padding: 14 }}>
                <svg width="100%" height="100%" viewBox="0 0 320 200">
                    <path d="M 10 100 Q 100 60 200 70 T 310 50" stroke="var(--accent)" strokeWidth="2" fill="none" />
                    {Array.from({ length: 21 }, (_, i) => {
                        const yo = (i - 10) * 4;
                        return <path key={i} d={`M 10 ${100 + yo} Q 100 ${60 + yo * 1.5} 200 ${70 + yo * 1.4} T 310 ${50 + yo * 2}`} stroke="var(--ink-3)" strokeWidth="0.6" fill="none" opacity="0.4" />;
                    })}
                    <text x="10" y="195" fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">14:00</text>
                    <text x="160" y="195" fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">17:00</text>
                    <text x="290" y="195" fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">21:00</text>
                </svg>
            </Box>
            <Box label="LEG-BY-LEG TABLE" style={{ padding: 14 }}>
                <table style={{ width: '100%', fontFamily: 'JetBrains Mono', fontSize: 11, borderCollapse: 'collapse' }}>
                    <thead>
                        <tr style={{ color: 'var(--ink-4)', textAlign: 'left' }}>
                            <th style={{ padding: '4px 0' }}>LEG</th><th>BRG</th><th>NM</th><th>TWA</th><th>VMG</th><th>MIN</th>
                        </tr>
                    </thead>
                    <tbody>
                        {[['S→1', '032°', '3.2', '38°', '6.8', '28'], ['1→2', '008°', '4.1', '62°', '7.2', '34'], ['2→F', '296°', '5.3', '110°', '7.0', '45']].map((r, i) => (
                            <tr key={i} style={{ borderTop: '1px dashed var(--hair)' }}>{r.map((c, j) => <td key={j} style={{ padding: '8px 0' }}>{c}</td>)}</tr>
                        ))}
                    </tbody>
                </table>
            </Box>
        </div>
    </SlideShell>
);

// IN-RACE — 4 variants of AI advisor treatment
const RaceCore = ({ children }) => (
    <Box style={{ position: 'relative', width: '100%', height: '100%', padding: 0 }}>
        <MapPlaceholder style={{ position: 'absolute', inset: 0 }}>
            <Isochrones cx={900} cy={500} />
            <RouteLine d="M 240 760 L 460 600 L 760 460 L 1040 320 L 1280 200" />
            <RouteLine d="M 240 760 L 440 660 L 740 520 L 1040 360 L 1280 200" dash color="var(--ink-3)" />
            {[[240, 760, '⊕'], [460, 600, '1'], [760, 460, '2'], [1040, 320, '3'], [1280, 200, 'F']].map(([x, y, n], i) => (
                <svg key={i} style={{ position: 'absolute', inset: 0, overflow: 'visible' }} width="100%" height="100%" viewBox="0 0 1840 1000" preserveAspectRatio="none">
                    <circle cx={x} cy={y} r={i === 0 ? 14 : 11} fill={i === 0 ? 'var(--accent)' : 'white'} stroke="var(--ink)" strokeWidth="2" />
                    <text x={x} y={y + 4} textAnchor="middle" fontFamily="JetBrains Mono" fontSize="11" fontWeight="700" fill={i === 0 ? 'white' : 'var(--ink)'}>{n}</text>
                </svg>
            ))}
            {/* AIS competitors */}
            {[[600, 520], [820, 440], [400, 680], [920, 380], [1100, 300]].map(([x, y], i) => (
                <svg key={`a${i}`} style={{ position: 'absolute', inset: 0, overflow: 'visible' }} width="100%" height="100%" viewBox="0 0 1840 1000" preserveAspectRatio="none">
                    <polygon points={`${x},${y - 8} ${x - 5},${y + 6} ${x + 5},${y + 6}`} fill="none" stroke="var(--ink-3)" strokeWidth="1.2" />
                    <text x={x + 8} y={y + 3} fontFamily="JetBrains Mono" fontSize="9" fill="var(--ink-4)">{['SHARK', 'OTTO', 'RUSH', 'VIRA', 'LADY'][i]}</text>
                </svg>
            ))}
            {[200, 600, 1000, 1400].map((x, i) => <WindBarb key={i} x={x} y={120} dir={210 + i * 4} />)}
        </MapPlaceholder>
        {children}
    </Box>
);

const RaceTopBar = () => (
    <div style={{ position: 'absolute', top: 16, left: 16, right: 16, display: 'flex', gap: 12 }}>
        <Box thin style={{ padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 14, background: 'var(--paper)' }}>
            <Chip live dot>LIVE</Chip>
            <Mono style={{ fontWeight: 700 }}>MORF · WED 14 MAY</Mono>
            <Lbl>LEG 2 / 4</Lbl>
        </Box>
        <Box thin style={{ padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 18, background: 'var(--paper)' }}>
            <Datum label="WIND" value="14.2 KT" />
            <Datum label="DIR" value="247°" />
            <Datum label="TWA" value="38°" />
            <Datum label="VMG" value="6.8 KT" />
            <Datum label="HDG" value="012°" />
        </Box>
        <div style={{ flex: 1 }} />
        <Box thin style={{ padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 14, background: 'var(--paper)' }}>
            <Datum label="ELAPSED" value="01:12:08" />
            <Datum label="CORR · PHRF" value="01:09:54" />
        </Box>
    </div>
);

const RaceBottomBar = () => (
    <div style={{ position: 'absolute', bottom: 16, left: 16, right: 16, display: 'flex', gap: 12 }}>
        <Box thin style={{ padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 18, background: 'var(--paper)' }}>
            <Lbl>NEXT</Lbl><Mono style={{ fontWeight: 700 }}>MARK 2</Mono>
            <Datum label="DIST" value="2.4 NM" />
            <Datum label="ETA" value="00:21:14" />
            <Datum label="LAYLINE" value="STBD · 4 MIN" />
        </Box>
        <div style={{ flex: 1 }} />
        <Btn sm>⛶ Cockpit mode</Btn>
        <Btn sm>⚙</Btn>
        <Btn solid sm>⏺ Recording · 01:12:08</Btn>
    </div>
);

// Race A — single big "current advice" card, refreshes
const RaceA = () => (
    <div className="rel" style={{ height: '100%' }}>
        <RaceCore>
            <RaceTopBar />
            <RaceBottomBar />
            {/* big single advisor card */}
            <Box style={{ position: 'absolute', left: 32, top: 120, width: 460, padding: 20, borderWidth: 2 }}>
                <div className="between" style={{ marginBottom: 10 }}>
                    <Chip live>AI ADVISOR · NOW</Chip>
                    <Mono style={{ fontSize: 11, color: 'var(--ink-4)' }}>NEXT IN 1:42</Mono>
                </div>
                <div style={{ fontSize: 24, fontWeight: 600, lineHeight: 1.25, letterSpacing: '-0.01em' }}>
                    Wind shifts <Mono style={{ background: 'var(--highlight)', padding: '0 4px' }}>−12°</Mono> in 18 min.
                    Tack now to be on the lifted tack at the layline.
                </div>
                <div className="rule" style={{ margin: '14px 0' }} />
                <Lbl style={{ marginBottom: 8 }}>WHY</Lbl>
                <ul style={{ margin: 0, padding: '0 0 0 16px', fontSize: 12, color: 'var(--ink-3)', lineHeight: 1.6 }}>
                    <li>17 of 21 ensemble members agree on a left shift before 16:00</li>
                    <li>Current heading 12° below optimal VMG</li>
                    <li>Layline reachable in 4 min on starboard</li>
                </ul>
                <div className="middle gap-8" style={{ marginTop: 14 }}>
                    <Btn sm solid>Acknowledge ✓</Btn>
                    <Btn sm>Snooze 5 min</Btn>
                    <Btn sm ghost>Why?</Btn>
                </div>
            </Box>
            <Callout x={520} y={180} w={260}><Note>single big card — focus on<br />ONE recommendation at a time.<br />least cognitive load.</Note></Callout>
        </RaceCore>
    </div>
);

// Race B — chat-thread / running log
const RaceB = () => (
    <div className="rel" style={{ height: '100%' }}>
        <RaceCore>
            <RaceTopBar />
            <RaceBottomBar />
            <Box style={{ position: 'absolute', right: 16, top: 90, bottom: 90, width: 400, padding: 0, display: 'flex', flexDirection: 'column' }}>
                <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--rule)' }}>
                    <Chip live>AI ADVISOR · THREAD</Chip>
                </div>
                <div style={{ flex: 1, overflow: 'hidden', padding: 14, display: 'flex', flexDirection: 'column', gap: 14 }}>
                    {[
                        { t: '13:42', s: 'Tack now. Wind shifts −12° in 18 min.', tag: 'TACK', latest: true },
                        { t: '13:38', s: 'You\'re sailing 6° below optimal VMG. Head up slightly.', tag: 'TRIM' },
                        { t: '13:24', s: 'Mark 1 layline in 6 min on starboard.', tag: 'LAYLINE' },
                        { t: '13:08', s: 'Conservative route now favored — 3 ensemble members shift right of forecast.', tag: 'ROUTE' },
                        { t: '12:54', s: 'Race start logged. Initial heading 032°.', tag: 'INFO' }
                    ].map((m, i) => (
                        <div key={i} style={{ opacity: m.latest ? 1 : 0.7 }}>
                            <div className="between">
                                <Chip>{m.tag}</Chip>
                                <Mono style={{ fontSize: 10, color: 'var(--ink-4)' }}>{m.t}</Mono>
                            </div>
                            <div style={{ fontSize: m.latest ? 16 : 13, fontWeight: m.latest ? 600 : 400, marginTop: 6, lineHeight: 1.35 }}>{m.s}</div>
                            {i < 4 && <div style={{ borderBottom: '1px dashed var(--hair)', marginTop: 14 }} />}
                        </div>
                    ))}
                </div>
                <div style={{ padding: 12, borderTop: '1px solid var(--rule)', display: 'flex', gap: 6 }}>
                    <Btn sm>Filter</Btn><Btn sm>Mute non-tactical</Btn>
                </div>
            </Box>
            <Callout x={1100} y={120} w={240}><Note>thread keeps history —<br />good for review &<br />"why did we tack?"</Note></Callout>
        </RaceCore>
    </div>
);

// Race C — top ticker
const RaceC = () => (
    <div className="rel" style={{ height: '100%' }}>
        <RaceCore>
            <Box style={{ position: 'absolute', top: 16, left: 16, right: 16, padding: '14px 18px', background: 'var(--ink)', borderColor: 'var(--ink)' }}>
                <div className="between">
                    <div className="middle gap-14">
                        <Chip style={{ background: 'var(--accent)', color: 'var(--paper)', borderColor: 'var(--accent)' }}>AI · NOW</Chip>
                        <span style={{ color: 'var(--paper)', fontSize: 18, fontWeight: 600 }}>
                            Wind shifts <Mono style={{ background: 'var(--accent)', color: 'var(--paper)', padding: '0 4px' }}>−12°</Mono> in 18 min · tack now to be on the lifted tack
                        </span>
                    </div>
                    <Mono style={{ color: 'rgba(255,255,255,0.6)', fontSize: 11 }}>↻ 1:42</Mono>
                </div>
            </Box>
            <RaceBottomBar />
            {/* mini telemetry on left */}
            <Box thin style={{ position: 'absolute', left: 16, top: 90, width: 200, padding: 14, background: 'var(--paper)' }}>
                <Datum label="WIND" value="14.2" big />
                <Datum label="DIR" value="247°" big />
                <Datum label="TWA" value="38°" big />
                <Datum label="VMG" value="6.8" big />
                <Datum label="HDG" value="012°" big />
            </Box>
            <Callout x={620} y={88} w={300}><Note>news-ticker style —<br />maximum map real estate<br />but advisory feels less central</Note></Callout>
        </RaceCore>
    </div>
);

// Race D — embedded speech bubbles on map
const RaceD = () => (
    <div className="rel" style={{ height: '100%' }}>
        <RaceCore>
            <RaceTopBar />
            <RaceBottomBar />
            {/* speech bubble pinned to the layline ahead */}
            <div style={{ position: 'absolute', left: '48%', top: '42%' }}>
                <Box style={{ padding: 14, width: 300, position: 'relative', borderWidth: 2 }}>
                    <Chip live style={{ marginBottom: 8 }}>TACTIC HERE</Chip>
                    <div style={{ fontSize: 15, fontWeight: 600, lineHeight: 1.3 }}>
                        Tack now — lifted tack at this layline in 4 min.
                    </div>
                    {/* bubble pointer */}
                    <svg style={{ position: 'absolute', bottom: -14, left: 32 }} width="20" height="16">
                        <polygon points="0,0 20,0 4,16" fill="var(--paper)" stroke="var(--ink)" strokeWidth="1.5" />
                    </svg>
                </Box>
            </div>
            <div style={{ position: 'absolute', left: '62%', top: '28%' }}>
                <Box thin style={{ padding: 10, width: 220 }}>
                    <Lbl>SHIFT FORECAST</Lbl>
                    <Mono style={{ fontSize: 13, fontWeight: 600 }}>−12° L · 16:08</Mono>
                    <Mono style={{ fontSize: 10, color: 'var(--ink-4)' }}>17/21 members agree</Mono>
                </Box>
            </div>
            <Callout x={32} y={120} w={260}><Note>map-anchored guidance —<br />spatial = where to do the<br />thing, not just what.</Note></Callout>
        </RaceCore>
    </div>
);

Object.assign(window, { PreA, PreB, PreC, RaceA, RaceB, RaceC, RaceD });
