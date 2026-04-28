/* Onboarding, signup, tier selection */

const OnbA = () => (
    <div className="full" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr' }}>
        <div style={{ background: 'var(--paper-2)', padding: 60, position: 'relative' }}>
            <div className="display" style={{ fontSize: 22 }}>SailLine<span style={{ color: 'var(--accent)' }}>.</span></div>
            <div style={{ marginTop: 80 }}>
                <Lbl>STEP 01 OF 04</Lbl>
                <h1 style={{ fontSize: 64, lineHeight: 1, letterSpacing: '-0.03em', margin: '12px 0 0', fontWeight: 700 }}>
                    Create your<br />SailLine account.
                </h1>
                <p style={{ fontSize: 16, color: 'var(--ink-3)', maxWidth: 440, marginTop: 24 }}>
                    You'll add your boat and pick a tier in the next two steps. Free is instant.
                </p>
            </div>
            {/* progress dots */}
            <div className="middle gap-8" style={{ position: 'absolute', bottom: 60, left: 60 }}>
                {['ACCOUNT', 'BOAT', 'HOME WATERS', 'PLAN'].map((s, i) => (
                    <div key={i} className="middle gap-6">
                        <div style={{ width: 28, height: 28, borderRadius: '50%', border: '1.5px solid var(--ink)', background: i === 0 ? 'var(--ink)' : 'transparent', color: i === 0 ? 'var(--paper)' : 'var(--ink)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                            <Mono style={{ fontSize: 11 }}>{i + 1}</Mono>
                        </div>
                        <Lbl>{s}</Lbl>
                        {i < 3 && <div style={{ width: 28, height: 1, background: 'var(--rule-2)' }} />}
                    </div>
                ))}
            </div>
        </div>
        <div style={{ padding: 60, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
            <div style={{ maxWidth: 460, width: '100%', margin: '0 auto' }}>
                {[
                    { l: 'EMAIL', v: 'skipper@example.com' },
                    { l: 'PASSWORD', v: '••••••••••••' },
                    { l: 'CALL SIGN / NAME', v: 'M. Hartwell' }
                ].map((f, i) => (
                    <div key={i} style={{ marginBottom: 20 }}>
                        <Lbl style={{ marginBottom: 6 }}>{f.l}</Lbl>
                        <div style={{ height: 44, border: '1.5px solid var(--ink)', borderRadius: 4, padding: '0 14px', display: 'flex', alignItems: 'center' }}>
                            <Mono style={{ fontSize: 14 }}>{f.v}</Mono>
                        </div>
                    </div>
                ))}
                <Btn solid lg style={{ width: '100%', marginTop: 12 }}>Continue →</Btn>
                <div className="middle gap-8" style={{ marginTop: 24, justifyContent: 'center' }}>
                    <Scrib w="80px" /><Lbl>OR</Lbl><Scrib w="80px" />
                </div>
                <Btn lg style={{ width: '100%', marginTop: 16 }}>Continue with Google</Btn>
            </div>
        </div>
    </div>
);

const OnbB = () => (
    <SlideShell num="ONB-02" title="Onboarding · Boat profile · Variant B (terminal-style)">
        <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr 320px', gap: 24, height: '100%' }}>
            {/* left: progress */}
            <Box label="SETUP" style={{ padding: 20 }}>
                {['ACCOUNT', 'BOAT CLASS', 'HOME WATERS', 'HANDICAP', 'PLAN'].map((s, i) => (
                    <div key={i} className="middle gap-12" style={{ padding: '12px 0', borderBottom: i < 4 ? '1px dashed var(--rule-2)' : 'none' }}>
                        <div style={{ width: 22, height: 22, borderRadius: '50%', background: i < 1 ? 'var(--ink)' : i === 1 ? 'var(--accent)' : 'transparent', border: '1.5px solid var(--ink)', color: 'var(--paper)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                            <Mono style={{ fontSize: 10 }}>{i < 1 ? '✓' : i + 1}</Mono>
                        </div>
                        <Mono style={{ fontSize: 12, fontWeight: i === 1 ? 700 : 400, color: i === 1 ? 'var(--ink)' : 'var(--ink-3)' }}>{s}</Mono>
                    </div>
                ))}
            </Box>

            {/* center: boat picker */}
            <div>
                <Lbl>STEP 02 / 05</Lbl>
                <h2 style={{ fontSize: 44, margin: '8px 0 6px', letterSpacing: '-0.02em' }}>Pick your boat class.</h2>
                <p style={{ color: 'var(--ink-3)', margin: 0 }}>Used to load polar curves. ML-learned curves replace these once you start recording.</p>

                <Box thin style={{ height: 44, marginTop: 20, padding: '0 14px', display: 'flex', alignItems: 'center' }}>
                    <Mono style={{ fontSize: 13, color: 'var(--ink-4)' }}>⌕  Search 8 classes…</Mono>
                </Box>

                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 12, marginTop: 16 }}>
                    {[
                        { n: 'Beneteau First 36.7', p: 'P0', s: 'PHRF 78', sel: true },
                        { n: 'J/105', p: 'P1', s: 'PHRF 87' },
                        { n: 'J/109', p: 'P1', s: 'PHRF 75' },
                        { n: 'J/111', p: 'P1', s: 'PHRF 51' },
                        { n: 'Farr 40', p: 'P1', s: 'PHRF 33' },
                        { n: 'Beneteau First 40.7', p: 'P1', s: 'PHRF 60' },
                        { n: 'Tartan 10', p: 'P1', s: 'PHRF 132' },
                        { n: 'Generic PHRF/ORC', p: 'P1', s: 'mid-range' },
                    ].map((b, i) => (
                        <Box key={i} thin style={{ padding: 14, borderColor: b.sel ? 'var(--ink)' : 'var(--rule-2)', borderWidth: b.sel ? 2 : 1, position: 'relative' }}>
                            <div className="between">
                                <Mono style={{ fontWeight: 700 }}>{b.n}</Mono>
                                <Chip>{b.p}</Chip>
                            </div>
                            <Lbl style={{ marginTop: 8 }}>{b.s} · 8 polar pts loaded</Lbl>
                            {b.sel && <Mono style={{ position: 'absolute', top: 8, right: 8, color: 'var(--accent)', fontSize: 10 }}>✓ SELECTED</Mono>}
                        </Box>
                    ))}
                </div>
                <div className="middle gap-12" style={{ marginTop: 24, justifyContent: 'flex-end' }}>
                    <Btn>← Back</Btn>
                    <Btn solid>Continue →</Btn>
                </div>
            </div>

            {/* right: preview polar */}
            <Box label="POLAR PREVIEW · BENETEAU 36.7" style={{ padding: 16 }}>
                <svg viewBox="0 0 280 280" width="100%">
                    <circle cx="140" cy="140" r="120" fill="none" stroke="var(--rule-2)" strokeWidth="0.8" strokeDasharray="2 4" />
                    <circle cx="140" cy="140" r="80" fill="none" stroke="var(--rule-2)" strokeWidth="0.8" strokeDasharray="2 4" />
                    <circle cx="140" cy="140" r="40" fill="none" stroke="var(--rule-2)" strokeWidth="0.8" strokeDasharray="2 4" />
                    <line x1="140" y1="20" x2="140" y2="260" stroke="var(--rule-2)" />
                    <line x1="20" y1="140" x2="260" y2="140" stroke="var(--rule-2)" />
                    <path d="M 140 140 Q 200 60 220 140 Q 220 220 160 240 Q 100 240 60 220 Q 20 140 60 60 Q 100 60 140 60" stroke="var(--ink)" fill="rgba(0,0,0,0.04)" strokeWidth="1.5" />
                </svg>
                <Datum label="WS 6 KT" value="4.2 / 6.0" />
                <Datum label="WS 10 KT" value="6.0 / 7.4" />
                <Datum label="WS 14 KT" value="6.6 / 7.9" />
                <Datum label="WS 20 KT" value="7.0 / 8.4" />
            </Box>
        </div>
    </SlideShell>
);

const OnbC = () => (
    <SlideShell num="ONB-03" title="Onboarding · Tier selection · Variant C (3-up comparison)">
        <div className="rel">
            <div style={{ textAlign: 'center', marginBottom: 32 }}>
                <Lbl>STEP 05 / 05 · CHOOSE YOUR PLAN</Lbl>
                <h2 style={{ fontSize: 56, margin: '12px 0 6px', letterSpacing: '-0.03em' }}>Free is plenty for pre-race.</h2>
                <p style={{ color: 'var(--ink-3)', margin: 0, fontSize: 16 }}>Upgrade when you hit the start line. You can switch tiers at any time.</p>
            </div>

            {/* monthly/annual toggle */}
            <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 24 }}>
                <Box thin style={{ padding: 4, display: 'flex', gap: 0 }}>
                    <Btn sm style={{ borderColor: 'transparent' }}>Monthly</Btn>
                    <Btn sm solid>Annual · save 17%</Btn>
                </Box>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 20, padding: '0 80px' }}>
                {[
                    { t: 'FREE', p: '$0', s: '— forever', f: ['Pre-race planning · all classes', '24-hour weather window', 'Single deterministic forecast', 'No AIS / no in-race routing', 'Account required'] },
                    { t: 'PRO', p: '$149', s: '/yr · save $31', hi: true, f: ['Everything in Free', 'In-race routing (inshore + distance)', '21-member ensemble forecasts', '7-day weather window', 'AIS competitor tracking', 'AI tactical advisor', 'PHRF · ORC · ORR-EZ · IRC · MORF', 'GPS track recording'] },
                    { t: 'HARDWARE', p: '$249', s: '/yr · + $200 hardware', f: ['Everything in Pro', 'Pi telemetry ingestion', 'True local wind routing', 'Current detection', 'Instrument-enhanced post-race', 'ML polar learning'] }
                ].map((tt, i) => (
                    <Box key={i} style={{ padding: 28, borderWidth: tt.hi ? 2.5 : 1.5, position: 'relative' }}>
                        {tt.hi && <Chip solid style={{ position: 'absolute', top: -12, left: 24, background: 'var(--accent)', borderColor: 'var(--accent)' }}>RECOMMENDED · MOST CLUB RACERS</Chip>}
                        <Mono style={{ fontWeight: 700, fontSize: 13 }}>{tt.t}</Mono>
                        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 16 }}>
                            <span style={{ fontSize: 56, fontWeight: 700, letterSpacing: '-0.04em' }}>{tt.p}</span>
                            <Mono style={{ color: 'var(--ink-3)' }}>{tt.s}</Mono>
                        </div>
                        <Btn solid={tt.hi} lg style={{ width: '100%', marginTop: 16 }}>{tt.t === 'FREE' ? 'Start free →' : 'Choose ' + tt.t.toLowerCase() + ' →'}</Btn>
                        <div style={{ marginTop: 20, fontSize: 13 }}>
                            {tt.f.map((x, j) => (
                                <div key={j} className="middle gap-8" style={{ padding: '6px 0', borderBottom: j < tt.f.length - 1 ? '1px dashed var(--hair)' : 'none' }}>
                                    <Mono style={{ color: 'var(--accent)' }}>+</Mono>
                                    <span>{x}</span>
                                </div>
                            ))}
                        </div>
                    </Box>
                ))}
            </div>

            <Callout x={1500} y={120} w={260}><Note>"Recommended" pill drives<br />conversions — center card<br />visually heaviest ✓</Note></Callout>
        </div>
    </SlideShell>
);

Object.assign(window, { OnbA, OnbB, OnbC });
