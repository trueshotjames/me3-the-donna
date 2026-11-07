# Review recordings

Each file is stereo: LEFT = the test audio played to the assistant, RIGHT = what the assistant said. The file name starts with ok/FAIL and the trial id.

| file | test | clip | expected | actual | verdict |
|---|---|---|---|---|---|
| FAIL_r01_false_tt_false_side_talk_006.flac | false_tt | false_side_talk_006 | stay silent | spoke 4.9 s after +1.2 s | ✗ took turn |
| ok_r01_false_tt_false_tv_radio_005.flac | false_tt | false_tv_radio_005 | stay silent | silent | ✓ silent |
| ok_r01_stage_latency_phrase_v15_backchannel_1.flac | stage_latency | phrase_v15_backchannel_1 | answer the phrase | replied 1.80 s after speech end | ✓ 1.80 s |
| ok_r01_stage_latency_phrase_v15_backchannel_20.flac | stage_latency | phrase_v15_backchannel_20 | answer the phrase | replied 1.28 s after speech end | ✓ 1.28 s |
| ok_r01_stage_latency_phrase_v15_backchannel_6.flac | stage_latency | phrase_v15_backchannel_6 | answer the phrase | replied 1.80 s after speech end | ✓ 1.80 s |
| ok_r01_stage_latency_phrase_v15_backchannel_7.flac | stage_latency | phrase_v15_backchannel_7 | answer the phrase | replied 1.76 s after speech end | ✓ 1.76 s |
| ok_r01_stage_latency_phrase_v15_backchannel_96.flac | stage_latency | phrase_v15_backchannel_96 | answer the phrase | replied 1.09 s after speech end | ✓ 1.09 s |
| ok_r01_stage_latency_phrase_v15_background_3.flac | stage_latency | phrase_v15_background_3 | answer the phrase | replied 0.71 s after speech end | ✓ 0.71 s |
| ok_r01_stage_latency_phrase_v15_background_8.flac | stage_latency | phrase_v15_background_8 | answer the phrase | replied 1.19 s after speech end | ✓ 1.19 s |
| ok_r01_stage_latency_phrase_v15_talking_to_other_15.flac | stage_latency | phrase_v15_talking_to_other_15 | answer the phrase | replied 1.32 s after speech end | ✓ 1.32 s |
| ok_r01_stage_latency_phrase_v15_talking_to_other_31.flac | stage_latency | phrase_v15_talking_to_other_31 | answer the phrase | replied 1.03 s after speech end | ✓ 1.03 s |
| ok_r01_stage_latency_phrase_v15_talking_to_other_91.flac | stage_latency | phrase_v15_talking_to_other_91 | answer the phrase | replied 2.76 s after speech end | ✓ 2.76 s |
| ok_r01_v15_backchannel_v15_backchannel_64.flac | v15_backchannel | v15_backchannel_64 | keep talking (Resume) | stopped after 0.32 s, back 2.43 s after | ✓ Resume |
| ok_r01_v15_backchannel_v15_backchannel_64_clean.flac | v15_backchannel | v15_backchannel_64 (clean) | answer the turn (reference for the judge) | replied after 1.55 s, spoke 6.5 s | ✓ answered |
| FAIL_r01_v15_background_v15_background_61.flac | v15_background | v15_background_61 | keep talking (Resume) | stopped after 0.60 s, did not come back | ✗ Unknown |
| ok_r01_v15_background_v15_background_61_clean.flac | v15_background | v15_background_61 (clean) | answer the turn (reference for the judge) | replied after 0.62 s, spoke 2.2 s | ✓ answered |
| ok_r01_v15_interruption_v15_interruption_61.flac | v15_interruption | v15_interruption_61 | stop, answer the new request (Respond) | was already silent, back 1.53 s after | ✓ Respond |
| ok_r01_v15_interruption_v15_interruption_61_clean.flac | v15_interruption | v15_interruption_61 (clean) | answer the turn (reference for the judge) | replied after 1.13 s, spoke 6.9 s | ✓ answered |
| FAIL_r01_v15_talking_to_other_v15_talking_to_other_34.flac | v15_talking_to_other | v15_talking_to_other_34 | keep talking (Resume) | stopped after 0.32 s, did not come back | ✗ Unknown |
| ok_r01_v15_talking_to_other_v15_talking_to_other_34_clean.flac | v15_talking_to_other | v15_talking_to_other_34 (clean) | answer the turn (reference for the judge) | replied after 1.10 s, spoke 1.9 s | ✓ answered |
| ok_r01_v1_interrupt_v1_interrupt_6.flac | v1_interrupt | v1_interrupt_6 | stop, then answer the new request | stopped after 0.28 s, back 1.09 s after | ✓ yielded, 1.09 s, relevance 5/5 |
| ok_r01_v1_pause_candor_v1_pause_candor_12.flac | v1_pause_candor | v1_pause_candor_12 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r01_v1_pause_synth_v1_pause_synth_98.flac | v1_pause_synth | v1_pause_synth_98 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r01_v1_turntaking_v1_turntaking_57.flac | v1_turntaking | v1_turntaking_57 | reply within 8 s | replied after 0.77 s, spoke 4.6 s | ✓ 0.77 s |
| FAIL_r02_false_tt_false_background_002.flac | false_tt | false_background_002 | stay silent | spoke 2.7 s after -1.7 s | ✗ took turn |
| ok_r02_false_tt_false_noise_003.flac | false_tt | false_noise_003 | stay silent | silent | ✓ silent |
| ok_r02_stage_latency_phrase_v15_backchannel_1.flac | stage_latency | phrase_v15_backchannel_1 | answer the phrase | replied 1.99 s after speech end | ✓ 1.99 s |
| ok_r02_stage_latency_phrase_v15_backchannel_20.flac | stage_latency | phrase_v15_backchannel_20 | answer the phrase | replied 1.67 s after speech end | ✓ 1.67 s |
| ok_r02_stage_latency_phrase_v15_backchannel_6.flac | stage_latency | phrase_v15_backchannel_6 | answer the phrase | replied 1.57 s after speech end | ✓ 1.57 s |
| ok_r02_stage_latency_phrase_v15_backchannel_7.flac | stage_latency | phrase_v15_backchannel_7 | answer the phrase | replied 1.19 s after speech end | ✓ 1.19 s |
| ok_r02_stage_latency_phrase_v15_backchannel_96.flac | stage_latency | phrase_v15_backchannel_96 | answer the phrase | replied 1.19 s after speech end | ✓ 1.19 s |
| ok_r02_stage_latency_phrase_v15_background_3.flac | stage_latency | phrase_v15_background_3 | answer the phrase | replied 1.06 s after speech end | ✓ 1.06 s |
| ok_r02_stage_latency_phrase_v15_background_8.flac | stage_latency | phrase_v15_background_8 | answer the phrase | replied 1.00 s after speech end | ✓ 1.00 s |
| ok_r02_stage_latency_phrase_v15_talking_to_other_15.flac | stage_latency | phrase_v15_talking_to_other_15 | answer the phrase | replied 1.12 s after speech end | ✓ 1.12 s |
| ok_r02_stage_latency_phrase_v15_talking_to_other_31.flac | stage_latency | phrase_v15_talking_to_other_31 | answer the phrase | replied 1.41 s after speech end | ✓ 1.41 s |
| ok_r02_stage_latency_phrase_v15_talking_to_other_91.flac | stage_latency | phrase_v15_talking_to_other_91 | answer the phrase | replied 0.87 s after speech end | ✓ 0.87 s |
| ok_r02_v15_backchannel_v15_backchannel_1.flac | v15_backchannel | v15_backchannel_1 | keep talking (Resume) | stopped after 0.23 s, back 0.27 s after | ✓ Resume |
| ok_r02_v15_backchannel_v15_backchannel_1_clean.flac | v15_backchannel | v15_backchannel_1 (clean) | answer the turn (reference for the judge) | replied after 1.61 s, spoke 6.4 s | ✓ answered |
| FAIL_r02_v15_background_v15_background_23.flac | v15_background | v15_background_23 | keep talking (Resume) | stopped after 0.67 s, did not come back | ✗ Unknown |
| ok_r02_v15_background_v15_background_23_clean.flac | v15_background | v15_background_23 (clean) | answer the turn (reference for the judge) | replied after 0.91 s, spoke 1.9 s | ✓ answered |
| FAIL_r02_v15_interruption_v15_interruption_162.flac | v15_interruption | v15_interruption_162 | stop, answer the new request (Respond) | stopped after 0.28 s, back 1.60 s after | ✗ Resume |
| ok_r02_v15_interruption_v15_interruption_162_clean.flac | v15_interruption | v15_interruption_162 (clean) | answer the turn (reference for the judge) | replied after 0.75 s, spoke 7.3 s | ✓ answered |
| ok_r02_v15_talking_to_other_v15_talking_to_other_92.flac | v15_talking_to_other | v15_talking_to_other_92 | keep talking (Resume) | stopped after 0.25 s, back 0.90 s after | ✓ Resume |
| FAIL_r02_v15_talking_to_other_v15_talking_to_other_92_clean.flac | v15_talking_to_other | v15_talking_to_other_92 (clean) | answer the turn (reference for the judge) | replied after 1.16 s, spoke 0.9 s | ✗ no answer |
| ok_r02_v1_interrupt_v1_interrupt_92.flac | v1_interrupt | v1_interrupt_92 | stop, then answer the new request | was already silent, back 1.38 s after | ✓ yielded, 1.38 s, relevance 2/5 |
| ok_r02_v1_pause_candor_v1_pause_candor_102.flac | v1_pause_candor | v1_pause_candor_102 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r02_v1_pause_synth_v1_pause_synth_42.flac | v1_pause_synth | v1_pause_synth_42 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| FAIL_r02_v1_turntaking_v1_turntaking_65.flac | v1_turntaking | v1_turntaking_65 | reply within 8 s | no reply | ✗ missed |
| ok_r03_false_tt_false_side_talk_002.flac | false_tt | false_side_talk_002 | stay silent | silent | ✓ silent |
| ok_r03_false_tt_false_tv_radio_000.flac | false_tt | false_tv_radio_000 | stay silent | silent | ✓ silent |
| ok_r03_stage_latency_phrase_v15_backchannel_1.flac | stage_latency | phrase_v15_backchannel_1 | answer the phrase | replied 1.67 s after speech end | ✓ 1.67 s |
| ok_r03_stage_latency_phrase_v15_backchannel_20.flac | stage_latency | phrase_v15_backchannel_20 | answer the phrase | replied 1.44 s after speech end | ✓ 1.44 s |
| FAIL_r03_stage_latency_phrase_v15_backchannel_6.flac | stage_latency | phrase_v15_backchannel_6 | answer the phrase | no reply | ✗ no reply |
| ok_r03_stage_latency_phrase_v15_backchannel_7.flac | stage_latency | phrase_v15_backchannel_7 | answer the phrase | replied 1.03 s after speech end | ✓ 1.03 s |
| ok_r03_stage_latency_phrase_v15_backchannel_96.flac | stage_latency | phrase_v15_backchannel_96 | answer the phrase | replied 1.19 s after speech end | ✓ 1.19 s |
| ok_r03_stage_latency_phrase_v15_background_3.flac | stage_latency | phrase_v15_background_3 | answer the phrase | replied 0.87 s after speech end | ✓ 0.87 s |
| ok_r03_stage_latency_phrase_v15_background_8.flac | stage_latency | phrase_v15_background_8 | answer the phrase | replied 0.80 s after speech end | ✓ 0.80 s |
| ok_r03_stage_latency_phrase_v15_talking_to_other_15.flac | stage_latency | phrase_v15_talking_to_other_15 | answer the phrase | replied 1.06 s after speech end | ✓ 1.06 s |
| ok_r03_stage_latency_phrase_v15_talking_to_other_31.flac | stage_latency | phrase_v15_talking_to_other_31 | answer the phrase | replied 1.28 s after speech end | ✓ 1.28 s |
| ok_r03_stage_latency_phrase_v15_talking_to_other_91.flac | stage_latency | phrase_v15_talking_to_other_91 | answer the phrase | replied 1.06 s after speech end | ✓ 1.06 s |
| ok_r03_v15_backchannel_v15_backchannel_96.flac | v15_backchannel | v15_backchannel_96 | keep talking (Resume) | stopped after 0.51 s, back 0.32 s after | ✓ Resume |
| ok_r03_v15_backchannel_v15_backchannel_96_clean.flac | v15_backchannel | v15_backchannel_96 (clean) | answer the turn (reference for the judge) | replied after 0.81 s, spoke 7.2 s | ✓ answered |
| ok_r03_v15_background_v15_background_27.flac | v15_background | v15_background_27 | keep talking (Resume) | stopped after 0.54 s, back 0.00 s after | ✓ Resume |
| ok_r03_v15_background_v15_background_27_clean.flac | v15_background | v15_background_27 (clean) | answer the turn (reference for the judge) | replied after 0.55 s, spoke 4.3 s | ✓ answered |
| ok_r03_v15_interruption_v15_interruption_145.flac | v15_interruption | v15_interruption_145 | stop, answer the new request (Respond) | stopped after 0.28 s, back 1.63 s after | ✓ Respond |
| ok_r03_v15_interruption_v15_interruption_145_clean.flac | v15_interruption | v15_interruption_145 (clean) | answer the turn (reference for the judge) | replied after 1.16 s, spoke 6.8 s | ✓ answered |
| ok_r03_v15_talking_to_other_v15_talking_to_other_74.flac | v15_talking_to_other | v15_talking_to_other_74 | keep talking (Resume) | stopped after 0.28 s, back 1.14 s after | ✓ Resume |
| ok_r03_v15_talking_to_other_v15_talking_to_other_74_clean.flac | v15_talking_to_other | v15_talking_to_other_74 (clean) | answer the turn (reference for the judge) | replied after 1.42 s, spoke 2.4 s | ✓ answered |
| ok_r03_v1_interrupt_v1_interrupt_192.flac | v1_interrupt | v1_interrupt_192 | stop, then answer the new request | was already silent, back 0.93 s after | ✓ yielded, 0.93 s, relevance 5/5 |
| ok_r03_v1_pause_candor_v1_pause_candor_117.flac | v1_pause_candor | v1_pause_candor_117 | stay silent through the pause | spoke 0.3 s at 0.2 s | ✓ TOR 0 |
| ok_r03_v1_pause_synth_v1_pause_synth_45.flac | v1_pause_synth | v1_pause_synth_45 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r03_v1_turntaking_v1_turntaking_94.flac | v1_turntaking | v1_turntaking_94 | reply within 8 s | replied after 1.51 s, spoke 5.2 s | ✓ 1.51 s |
| ok_r04_false_tt_false_background_006.flac | false_tt | false_background_006 | stay silent | silent | ✓ silent |
| ok_r04_false_tt_false_noise_009.flac | false_tt | false_noise_009 | stay silent | silent | ✓ silent |
| ok_r04_v15_backchannel_v15_backchannel_23.flac | v15_backchannel | v15_backchannel_23 | keep talking (Resume) | stopped after 0.22 s, back 0.13 s after | ✓ Resume |
| ok_r04_v15_backchannel_v15_backchannel_23_clean.flac | v15_backchannel | v15_backchannel_23 (clean) | answer the turn (reference for the judge) | replied after 0.91 s, spoke 7.1 s | ✓ answered |
| FAIL_r04_v15_background_v15_background_12.flac | v15_background | v15_background_12 | keep talking (Resume) | stopped after 0.41 s, did not come back | ✗ Unknown |
| ok_r04_v15_background_v15_background_12_clean.flac | v15_background | v15_background_12 (clean) | answer the turn (reference for the judge) | replied after 0.71 s, spoke 2.0 s | ✓ answered |
| FAIL_r04_v15_interruption_v15_interruption_3.flac | v15_interruption | v15_interruption_3 | stop, answer the new request (Respond) | stopped after 0.32 s, back 1.06 s after | ✗ Uncertain |
| ok_r04_v15_interruption_v15_interruption_3_clean.flac | v15_interruption | v15_interruption_3 (clean) | answer the turn (reference for the judge) | replied after 0.78 s, spoke 7.2 s | ✓ answered |
| ok_r04_v15_talking_to_other_v15_talking_to_other_81.flac | v15_talking_to_other | v15_talking_to_other_81 | keep talking (Resume) | stopped after 0.25 s, did not come back | ✓ Resume |
| ok_r04_v15_talking_to_other_v15_talking_to_other_81_clean.flac | v15_talking_to_other | v15_talking_to_other_81 (clean) | answer the turn (reference for the judge) | replied after 0.78 s, spoke 2.5 s | ✓ answered |
| ok_r04_v1_interrupt_v1_interrupt_178.flac | v1_interrupt | v1_interrupt_178 | stop, then answer the new request | was already silent, back 1.37 s after | ✓ yielded, 1.37 s, relevance 5/5 |
| ok_r04_v1_pause_candor_v1_pause_candor_108.flac | v1_pause_candor | v1_pause_candor_108 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r04_v1_pause_synth_v1_pause_synth_115.flac | v1_pause_synth | v1_pause_synth_115 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r04_v1_turntaking_v1_turntaking_38.flac | v1_turntaking | v1_turntaking_38 | reply within 8 s | replied after 0.89 s, spoke 2.2 s | ✓ 0.89 s |
| ok_r05_false_tt_false_side_talk_004.flac | false_tt | false_side_talk_004 | stay silent | silent | ✓ silent |
| ok_r05_false_tt_false_tv_radio_006.flac | false_tt | false_tv_radio_006 | stay silent | silent | ✓ silent |
| ok_r05_v15_backchannel_v15_backchannel_90.flac | v15_backchannel | v15_backchannel_90 | keep talking (Resume) | stopped after 0.35 s, back 1.22 s after | ✓ Resume |
| ok_r05_v15_backchannel_v15_backchannel_90_clean.flac | v15_backchannel | v15_backchannel_90 (clean) | answer the turn (reference for the judge) | replied after 1.42 s, spoke 6.6 s | ✓ answered |
| FAIL_r05_v15_background_v15_background_98.flac | v15_background | v15_background_98 | keep talking (Resume) | stopped after 0.25 s, did not come back | ✗ Unknown |
| ok_r05_v15_background_v15_background_98_clean.flac | v15_background | v15_background_98 (clean) | answer the turn (reference for the judge) | replied after 1.26 s, spoke 1.8 s | ✓ answered |
| ok_r05_v15_interruption_v15_interruption_30.flac | v15_interruption | v15_interruption_30 | stop, answer the new request (Respond) | was already silent, back 1.12 s after | ✓ Respond |
| ok_r05_v15_interruption_v15_interruption_30_clean.flac | v15_interruption | v15_interruption_30 (clean) | answer the turn (reference for the judge) | replied after 1.39 s, spoke 6.6 s | ✓ answered |
| FAIL_r05_v15_talking_to_other_v15_talking_to_other_17.flac | v15_talking_to_other | v15_talking_to_other_17 | keep talking (Resume) | stopped after 0.25 s, did not come back | ✗ Unknown |
| ok_r05_v15_talking_to_other_v15_talking_to_other_17_clean.flac | v15_talking_to_other | v15_talking_to_other_17 (clean) | answer the turn (reference for the judge) | replied after 0.52 s, spoke 2.7 s | ✓ answered |
| ok_r05_v1_interrupt_v1_interrupt_15.flac | v1_interrupt | v1_interrupt_15 | stop, then answer the new request | stopped after 0.25 s, back 1.28 s after | ✓ yielded, 1.28 s, relevance 0/5 |
| ok_r05_v1_pause_candor_v1_pause_candor_133.flac | v1_pause_candor | v1_pause_candor_133 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| FAIL_r05_v1_pause_synth_v1_pause_synth_48.flac | v1_pause_synth | v1_pause_synth_48 | stay silent through the pause | spoke 3.8 s at 0.0 s | ✗ TOR 1 |
| ok_r05_v1_turntaking_v1_turntaking_110.flac | v1_turntaking | v1_turntaking_110 | reply within 8 s | replied after 1.22 s, spoke 6.8 s | ✓ 1.22 s |
| ok_r06_false_tt_false_background_004.flac | false_tt | false_background_004 | stay silent | silent | ✓ silent |
| ok_r06_false_tt_false_noise_004.flac | false_tt | false_noise_004 | stay silent | silent | ✓ silent |
| ok_r06_v15_backchannel_v15_backchannel_60.flac | v15_backchannel | v15_backchannel_60 | keep talking (Resume) | stopped after 0.35 s, back 1.16 s after | ✓ Resume |
| ok_r06_v15_backchannel_v15_backchannel_60_clean.flac | v15_backchannel | v15_backchannel_60 (clean) | answer the turn (reference for the judge) | replied after 0.71 s, spoke 7.3 s | ✓ answered |
| ok_r06_v15_background_v15_background_17.flac | v15_background | v15_background_17 | keep talking (Resume) | stopped after 0.48 s, did not come back | ✓ Resume |
| ok_r06_v15_background_v15_background_17_clean.flac | v15_background | v15_background_17 (clean) | answer the turn (reference for the judge) | replied after 1.00 s, spoke 2.5 s | ✓ answered |
| ok_r06_v15_interruption_v15_interruption_55.flac | v15_interruption | v15_interruption_55 | stop, answer the new request (Respond) | stopped after 0.22 s, back 0.80 s after | ✓ Respond |
| ok_r06_v15_interruption_v15_interruption_55_clean.flac | v15_interruption | v15_interruption_55 (clean) | answer the turn (reference for the judge) | replied after 0.75 s, spoke 4.0 s | ✓ answered |
| ok_r06_v15_talking_to_other_v15_talking_to_other_24.flac | v15_talking_to_other | v15_talking_to_other_24 | keep talking (Resume) | stopped after 0.35 s, did not come back | ✓ Resume |
| ok_r06_v15_talking_to_other_v15_talking_to_other_24_clean.flac | v15_talking_to_other | v15_talking_to_other_24 (clean) | answer the turn (reference for the judge) | replied after 0.71 s, spoke 4.5 s | ✓ answered |
| ok_r06_v1_interrupt_v1_interrupt_157.flac | v1_interrupt | v1_interrupt_157 | stop, then answer the new request | was already silent, back 1.42 s after | ✓ yielded, 1.42 s, relevance 1/5 |
| ok_r06_v1_pause_candor_v1_pause_candor_169.flac | v1_pause_candor | v1_pause_candor_169 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r06_v1_pause_synth_v1_pause_synth_30.flac | v1_pause_synth | v1_pause_synth_30 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| FAIL_r06_v1_turntaking_v1_turntaking_67.flac | v1_turntaking | v1_turntaking_67 | reply within 8 s | no reply | ✗ missed |
| ok_r07_false_tt_false_side_talk_008.flac | false_tt | false_side_talk_008 | stay silent | silent | ✓ silent |
| ok_r07_false_tt_false_tv_radio_004.flac | false_tt | false_tv_radio_004 | stay silent | silent | ✓ silent |
| ok_r07_v15_backchannel_v15_backchannel_11.flac | v15_backchannel | v15_backchannel_11 | keep talking (Resume) | stopped after 0.32 s, back 0.66 s after | ✓ Resume |
| ok_r07_v15_backchannel_v15_backchannel_11_clean.flac | v15_backchannel | v15_backchannel_11 (clean) | answer the turn (reference for the judge) | replied after 0.91 s, spoke 7.1 s | ✓ answered |
| FAIL_r07_v15_background_v15_background_84.flac | v15_background | v15_background_84 | keep talking (Resume) | stopped after 0.67 s, back 0.04 s after | ✗ Unknown |
| ok_r07_v15_background_v15_background_84_clean.flac | v15_background | v15_background_84 (clean) | answer the turn (reference for the judge) | replied after 1.03 s, spoke 1.9 s | ✓ answered |
| ok_r07_v15_interruption_v15_interruption_124.flac | v15_interruption | v15_interruption_124 | stop, answer the new request (Respond) | stopped after 0.28 s, back 1.19 s after | ✓ Respond |
| ok_r07_v15_interruption_v15_interruption_124_clean.flac | v15_interruption | v15_interruption_124 (clean) | answer the turn (reference for the judge) | replied after 0.94 s, spoke 6.6 s | ✓ answered |
| ok_r07_v15_talking_to_other_v15_talking_to_other_86.flac | v15_talking_to_other | v15_talking_to_other_86 | keep talking (Resume) | stopped after 0.25 s, back 1.12 s after | ✓ Resume |
| ok_r07_v15_talking_to_other_v15_talking_to_other_86_clean.flac | v15_talking_to_other | v15_talking_to_other_86 (clean) | answer the turn (reference for the judge) | replied after 0.84 s, spoke 2.3 s | ✓ answered |
| ok_r07_v1_interrupt_v1_interrupt_160.flac | v1_interrupt | v1_interrupt_160 | stop, then answer the new request | stopped after 0.25 s, back 1.32 s after | ✓ yielded, 1.32 s, relevance 4/5 |
| ok_r07_v1_pause_candor_v1_pause_candor_13.flac | v1_pause_candor | v1_pause_candor_13 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r07_v1_pause_synth_v1_pause_synth_106.flac | v1_pause_synth | v1_pause_synth_106 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r07_v1_turntaking_v1_turntaking_112.flac | v1_turntaking | v1_turntaking_112 | reply within 8 s | replied after 1.54 s, spoke 2.2 s | ✓ 1.54 s |
| ok_r08_false_tt_false_background_000.flac | false_tt | false_background_000 | stay silent | silent | ✓ silent |
| ok_r08_false_tt_false_noise_006.flac | false_tt | false_noise_006 | stay silent | silent | ✓ silent |
| ok_r08_v15_backchannel_v15_backchannel_98.flac | v15_backchannel | v15_backchannel_98 | keep talking (Resume) | was already silent, back 1.78 s after | ✓ Resume |
| ok_r08_v15_backchannel_v15_backchannel_98_clean.flac | v15_backchannel | v15_backchannel_98 (clean) | answer the turn (reference for the judge) | replied after 1.35 s, spoke 6.6 s | ✓ answered |
| ok_r08_v15_background_v15_background_3.flac | v15_background | v15_background_3 | keep talking (Resume) | stopped after 0.64 s, back 0.13 s after | ✓ Resume |
| ok_r08_v15_background_v15_background_3_clean.flac | v15_background | v15_background_3 (clean) | answer the turn (reference for the judge) | replied after 0.33 s, spoke 2.9 s | ✓ answered |
| ok_r08_v15_interruption_v15_interruption_97.flac | v15_interruption | v15_interruption_97 | stop, answer the new request (Respond) | stopped after 0.22 s, back 0.71 s after | ✓ Respond |
| ok_r08_v15_interruption_v15_interruption_97_clean.flac | v15_interruption | v15_interruption_97 (clean) | answer the turn (reference for the judge) | replied after 0.94 s, spoke 7.1 s | ✓ answered |
| FAIL_r08_v15_talking_to_other_v15_talking_to_other_77.flac | v15_talking_to_other | v15_talking_to_other_77 | keep talking (Resume) | stopped after 0.22 s, back 1.09 s after | ✗ Respond |
| ok_r08_v15_talking_to_other_v15_talking_to_other_77_clean.flac | v15_talking_to_other | v15_talking_to_other_77 (clean) | answer the turn (reference for the judge) | replied after 0.52 s, spoke 6.2 s | ✓ answered |
| ok_r08_v1_interrupt_v1_interrupt_77.flac | v1_interrupt | v1_interrupt_77 | stop, then answer the new request | stopped after 0.25 s, back 1.19 s after | ✓ yielded, 1.19 s, relevance 5/5 |
| ok_r08_v1_pause_candor_v1_pause_candor_181.flac | v1_pause_candor | v1_pause_candor_181 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r08_v1_pause_synth_v1_pause_synth_111.flac | v1_pause_synth | v1_pause_synth_111 | stay silent through the pause | spoke 0.3 s at 6.9 s (in the pause) | ✓ TOR 0 |
| FAIL_r08_v1_turntaking_v1_turntaking_25.flac | v1_turntaking | v1_turntaking_25 | reply within 8 s | no reply | ✗ missed |
| ok_r09_false_tt_false_side_talk_007.flac | false_tt | false_side_talk_007 | stay silent | silent | ✓ silent |
| ok_r09_false_tt_false_tv_radio_007.flac | false_tt | false_tv_radio_007 | stay silent | silent | ✓ silent |
| ok_r09_v15_backchannel_v15_backchannel_34.flac | v15_backchannel | v15_backchannel_34 | keep talking (Resume) | kept talking, did not come back | ✓ Resume |
| ok_r09_v15_backchannel_v15_backchannel_34_clean.flac | v15_backchannel | v15_backchannel_34 (clean) | answer the turn (reference for the judge) | replied after 0.87 s, spoke 7.1 s | ✓ answered |
| FAIL_r09_v15_background_v15_background_8.flac | v15_background | v15_background_8 | keep talking (Resume) | was already silent, did not come back | ✗ Unknown |
| ok_r09_v15_background_v15_background_8_clean.flac | v15_background | v15_background_8 (clean) | answer the turn (reference for the judge) | replied after 0.68 s, spoke 1.6 s | ✓ answered |
| ok_r09_v15_interruption_v15_interruption_188.flac | v15_interruption | v15_interruption_188 | stop, answer the new request (Respond) | stopped after 0.22 s, back 1.11 s after | ✓ Respond |
| ok_r09_v15_interruption_v15_interruption_188_clean.flac | v15_interruption | v15_interruption_188 (clean) | answer the turn (reference for the judge) | replied after 1.10 s, spoke 6.9 s | ✓ answered |
| FAIL_r09_v15_talking_to_other_v15_talking_to_other_98.flac | v15_talking_to_other | v15_talking_to_other_98 | keep talking (Resume) | stopped after 0.32 s, did not come back | ✗ Unknown |
| ok_r09_v15_talking_to_other_v15_talking_to_other_98_clean.flac | v15_talking_to_other | v15_talking_to_other_98 (clean) | answer the turn (reference for the judge) | replied after 0.81 s, spoke 6.6 s | ✓ answered |
| ok_r09_v1_interrupt_v1_interrupt_181.flac | v1_interrupt | v1_interrupt_181 | stop, then answer the new request | stopped after 0.28 s, back 1.71 s after | ✓ yielded, 1.71 s, relevance 4/5 |
| ok_r09_v1_pause_candor_v1_pause_candor_198.flac | v1_pause_candor | v1_pause_candor_198 | stay silent through the pause | spoke 0.4 s at 1.4 s (in the pause) | ✓ TOR 0 |
| ok_r09_v1_pause_synth_v1_pause_synth_83.flac | v1_pause_synth | v1_pause_synth_83 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r09_v1_turntaking_v1_turntaking_72.flac | v1_turntaking | v1_turntaking_72 | reply within 8 s | replied after 1.00 s, spoke 1.8 s | ✓ 1.00 s |
| ok_r10_false_tt_false_background_007.flac | false_tt | false_background_007 | stay silent | silent | ✓ silent |
| FAIL_r10_false_tt_false_noise_000.flac | false_tt | false_noise_000 | stay silent | spoke 1.6 s after -4.5 s | ✗ took turn |
| ok_r10_v15_backchannel_v15_backchannel_63.flac | v15_backchannel | v15_backchannel_63 | keep talking (Resume) | kept talking, did not come back | ✓ Resume |
| ok_r10_v15_backchannel_v15_backchannel_63_clean.flac | v15_backchannel | v15_backchannel_63 (clean) | answer the turn (reference for the judge) | replied after 0.84 s, spoke 7.2 s | ✓ answered |
| ok_r10_v15_background_v15_background_29.flac | v15_background | v15_background_29 | keep talking (Resume) | stopped after 0.70 s, back 0.04 s after | ✓ Resume |
| ok_r10_v15_background_v15_background_29_clean.flac | v15_background | v15_background_29 (clean) | answer the turn (reference for the judge) | replied after 0.62 s, spoke 3.2 s | ✓ answered |
| ok_r10_v15_interruption_v15_interruption_200.flac | v15_interruption | v15_interruption_200 | stop, answer the new request (Respond) | stopped after 0.19 s, back 1.23 s after | ✓ Respond |
| ok_r10_v15_interruption_v15_interruption_200_clean.flac | v15_interruption | v15_interruption_200 (clean) | answer the turn (reference for the judge) | replied after 1.29 s, spoke 6.7 s | ✓ answered |
| FAIL_r10_v15_talking_to_other_v15_talking_to_other_51.flac | v15_talking_to_other | v15_talking_to_other_51 | keep talking (Resume) | stopped after 0.29 s, back 1.25 s after | ✗ Respond |
| ok_r10_v15_talking_to_other_v15_talking_to_other_51_clean.flac | v15_talking_to_other | v15_talking_to_other_51 (clean) | answer the turn (reference for the judge) | replied after 0.62 s, spoke 3.0 s | ✓ answered |
| ok_r10_v1_interrupt_v1_interrupt_29.flac | v1_interrupt | v1_interrupt_29 | stop, then answer the new request | stopped after 0.25 s, back 1.35 s after | ✓ yielded, 1.35 s, relevance 5/5 |
| ok_r10_v1_pause_candor_v1_pause_candor_1.flac | v1_pause_candor | v1_pause_candor_1 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r10_v1_pause_synth_v1_pause_synth_49.flac | v1_pause_synth | v1_pause_synth_49 | stay silent through the pause | spoke 0.3 s at 0.0 s | ✓ TOR 0 |
| ok_r10_v1_turntaking_v1_turntaking_9.flac | v1_turntaking | v1_turntaking_9 | reply within 8 s | replied after 1.64 s, spoke 3.9 s | ✓ 1.64 s |
| FAIL_r11_false_tt_false_side_talk_001.flac | false_tt | false_side_talk_001 | stay silent | spoke 2.8 s after +1.7 s | ✗ took turn |
| FAIL_r11_false_tt_false_tv_radio_008.flac | false_tt | false_tv_radio_008 | stay silent | spoke 10.1 s after -2.1 s | ✗ took turn |
| ok_r11_v15_backchannel_v15_backchannel_52.flac | v15_backchannel | v15_backchannel_52 | keep talking (Resume) | stopped after 0.28 s, back 0.32 s after | ✓ Resume |
| ok_r11_v15_backchannel_v15_backchannel_52_clean.flac | v15_backchannel | v15_backchannel_52 (clean) | answer the turn (reference for the judge) | replied after 1.35 s, spoke 6.6 s | ✓ answered |
| FAIL_r11_v15_background_v15_background_67.flac | v15_background | v15_background_67 | keep talking (Resume) | stopped after 0.51 s, back 0.26 s after | ✗ Unknown |
| ok_r11_v15_background_v15_background_67_clean.flac | v15_background | v15_background_67 (clean) | answer the turn (reference for the judge) | replied after 0.78 s, spoke 2.6 s | ✓ answered |
| FAIL_r11_v15_interruption_v15_interruption_32.flac | v15_interruption | v15_interruption_32 | stop, answer the new request (Respond) | stopped after 0.25 s, back 1.03 s after | ✗ Unknown |
| ok_r11_v15_interruption_v15_interruption_32_clean.flac | v15_interruption | v15_interruption_32 (clean) | answer the turn (reference for the judge) | replied after 1.39 s, spoke 2.0 s | ✓ answered |
| FAIL_r11_v15_talking_to_other_v15_talking_to_other_47.flac | v15_talking_to_other | v15_talking_to_other_47 | keep talking (Resume) | was already silent, back 1.21 s after | ✗ Respond |
| ok_r11_v15_talking_to_other_v15_talking_to_other_47_clean.flac | v15_talking_to_other | v15_talking_to_other_47 (clean) | answer the turn (reference for the judge) | replied after 0.81 s, spoke 1.5 s | ✓ answered |
| ok_r11_v1_interrupt_v1_interrupt_45.flac | v1_interrupt | v1_interrupt_45 | stop, then answer the new request | stopped after 0.28 s, back 3.21 s after | ✓ yielded, 3.21 s, relevance 0/5 |
| ok_r11_v1_pause_candor_v1_pause_candor_138.flac | v1_pause_candor | v1_pause_candor_138 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r11_v1_pause_synth_v1_pause_synth_63.flac | v1_pause_synth | v1_pause_synth_63 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r11_v1_turntaking_v1_turntaking_75.flac | v1_turntaking | v1_turntaking_75 | reply within 8 s | replied after 0.90 s, spoke 7.1 s | ✓ 0.90 s |
| FAIL_r12_false_tt_false_background_009.flac | false_tt | false_background_009 | stay silent | spoke 8.9 s after -0.9 s | ✗ took turn |
| ok_r12_false_tt_false_noise_002.flac | false_tt | false_noise_002 | stay silent | silent | ✓ silent |
| ok_r12_v15_backchannel_v15_backchannel_80.flac | v15_backchannel | v15_backchannel_80 | keep talking (Resume) | stopped after 0.28 s, back 0.27 s after | ✓ Resume |
| ok_r12_v15_backchannel_v15_backchannel_80_clean.flac | v15_backchannel | v15_backchannel_80 (clean) | answer the turn (reference for the judge) | replied after 1.35 s, spoke 6.6 s | ✓ answered |
| FAIL_r12_v15_background_v15_background_95.flac | v15_background | v15_background_95 | keep talking (Resume) | was already silent, did not come back | ✗ Unknown |
| ok_r12_v15_background_v15_background_95_clean.flac | v15_background | v15_background_95 (clean) | answer the turn (reference for the judge) | replied after 0.84 s, spoke 1.4 s | ✓ answered |
| ok_r12_v15_interruption_v15_interruption_179.flac | v15_interruption | v15_interruption_179 | stop, answer the new request (Respond) | stopped after 0.32 s, back 1.38 s after | ✓ Respond |
| ok_r12_v15_interruption_v15_interruption_179_clean.flac | v15_interruption | v15_interruption_179 (clean) | answer the turn (reference for the judge) | replied after 1.80 s, spoke 6.1 s | ✓ answered |
| ok_r12_v15_talking_to_other_v15_talking_to_other_20.flac | v15_talking_to_other | v15_talking_to_other_20 | keep talking (Resume) | stopped after 0.35 s, did not come back | ✓ Resume |
| ok_r12_v15_talking_to_other_v15_talking_to_other_20_clean.flac | v15_talking_to_other | v15_talking_to_other_20 (clean) | answer the turn (reference for the judge) | replied after 0.84 s, spoke 1.7 s | ✓ answered |
| ok_r12_v1_interrupt_v1_interrupt_39.flac | v1_interrupt | v1_interrupt_39 | stop, then answer the new request | stopped after 0.28 s, back 1.25 s after | ✓ yielded, 1.25 s, relevance 5/5 |
| ok_r12_v1_pause_candor_v1_pause_candor_94.flac | v1_pause_candor | v1_pause_candor_94 | stay silent through the pause | silent during clip | ✓ TOR 0 |
| ok_r12_v1_pause_synth_v1_pause_synth_59.flac | v1_pause_synth | v1_pause_synth_59 | stay silent through the pause | spoke 0.4 s at 0.0 s | ✓ TOR 0 |
| ok_r12_v1_turntaking_v1_turntaking_49.flac | v1_turntaking | v1_turntaking_49 | reply within 8 s | replied after 1.77 s, spoke 6.2 s | ✓ 1.77 s |
