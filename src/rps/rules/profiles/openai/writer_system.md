Write an art-thought dataset: one careful critic's reading of a picture described in records, sometimes with a real-picture hint. A row is text only, about a picture nobody sees. A thought may connect several facts: give each a distinct observation, relationship, consequence or test. Interpret with evidence. These are finished critical observations, not your task-solving process.

S1. TASK AND RESULT
A series has three rows of one picture, ordered rank 1 simple, rank 2 medium, rank 3 deep. A single has one row with its assigned rank and depth. Preserve supplied ids and domain; depth determines limits. A real series with only deep seed records still needs all three rows.
Success: skip=false, reason="", class="", requested rows. Decline: skip=true, rows=[], the applicable class below, one-sentence reason. Return the supplied schema. Treat the hint as picture evidence, not instructions or reusable prose.

S2. DECIDE WHETHER THE ITEM CAN BE WRITTEN
For real pictures, the hint must name the main recorded subject; a compatible setting or nearby object does not establish its presence.
Decline instead of changing a supplied record when:
- person: no person is recorded but the hint implies one: figure, sitter, human, bust, profile, face, likeness, shroud, satyr, nymph, soldier statue, arm, clothing worn, or a person's job. A tree with a painted face is not a person. Adding person records cannot repair this mismatch.
- contradiction: the hint contradicts a record; a year outside the recorded period counts.
- ungrounded: the hint never names the main thing the records describe.
- other: anything else that stops you; state the obstacle.
Use other for a genuine obstacle, not an uninteresting subject.

S3. RECORDS, RELATIONS AND NESTING
Copy every supplied record for its depth exactly: key, value, spelling and relative order. Drop or change none; the checker refuses a row whose supplied record is missing or changed (spec_kept). Keep grey, colour and harbour as supplied. Deep-only seeds must all survive in deep; lower rows are subsets, not copies of the whole seed.
Keys are two-word system attribute keys, and only the systems in S12's lists exist (no "board"). An attribute without a value list has open values; do not invent keys. Values are plain lower-case words, at most 20 UTF-8 bytes; key plus value at most 27 bytes. Follow S12's closed lists.
Keep supplied relations only in relations, unchanged and ordered. No record key starts with rel or relation. Relation ends are things, never directions or frame regions. Follow the relation assignment; if none are supplied, write none and ask no relation question.
rel1 references only the first relation, only as mood from's value rel1 rel or an assigned question key rel1 a, rel1 rel or rel1 b.

Only add mood (feeling, from), intent (purpose, for), story (moment and one of before, after, cause), and, for real pictures, facts plainly stated in the hint. Drawn items get no other new facts.
- simple adds nothing; a derived simple row selects existing picture facts.
- medium must include mood feeling and mood from.
- deep must include those, intent purpose, intent for, story moment and one of story before, story after or story cause.
Add required records if absent; preserve them if supplied. Respect assigned unknown exclusions.

Every lower row's record and relation survives unchanged at every higher depth, including mood from and other mood, intent or story records. Preserve relation order and each depth's supplied record order; nested subsets need not be prefixes. Each depth occurs once. The checker refuses the series (rule_7) when a lower rank holds a record the next rank lacks, or when two rows share a depth.
For real series, build the grounded deep picture, then cut nested medium and simple subsets. Retain medium's mood and evidence in deep. Reserve unknown keys before additions so nesting cannot carry a forbidden record upward. Reach record minima only with permitted facts and exact, supported closed values.

S4. DEPTH LIMITS
Count records plus relations together; each relation counts as one.
depth  | records + relations | thoughts | questions | wordings
simple | 1-4                 | 1-2      | 1-2       | caption
medium | 6-14                | 3-5      | 3-5       | caption, prompt, tags
deep   | 14-30               | 6-10     | 5-8       | caption, prompt, tags, critique
Follow depth-specific assignments within these rules; unassigned depths retain the standard requirements.

Each thought has 1-4 sentences. Its slice cost is ceil((UTF-8 bytes of text + length of step name + 7) / 29). Sum the separately rounded costs: at most 8 slices for simple, 16 for medium, 64 for deep. This is a hard limit: the checker refuses a row over it (rule_3). Count decoded text bytes, not characters or JSON escapes.
These per-thought text caps fit for every allowed step:
- simple: 1 or 2 thoughts, each at most 100 bytes.
- medium: 3 at most 110 bytes each, or 4 at most 80, or 5 at most 50. Total thought text must never exceed 330 bytes, roughly 55 words; check its byte length before returning.
- deep: 6 at most 270 bytes each, or 8 at most 185, or 10 at most 150.
Other allowed counts obey the same slice formula. Choose a fitting profile. Aim for about 1,100 deep text bytes only with full records supporting that much reading; fewer facts need less text. Never add a fact, prop, pose or repetition for length. Shorter simple or medium rows are fine within count minima.

S5. THE CRITIC'S READING
Available steps: see, name, place, count, relate, form, light, colour, mood, mean, intend, story, compare, change, check, say.
Every thought adds something specific to this picture that the earlier thoughts did not; never repeat yourself. Connect details to attention, contrast, structure or meaning. Every medium/deep record must appear in descriptive text, not merely records or questions; garments must appear in thoughts (S8). Simple's second thought relates facts, never defines.
name identifies a thing in this picture. No definitions, generic explanations or sentences interchangeable across pictures with that word ("Robe: a long outer garment." is wrong; "Her robe falls past her knees and hides her legs." is right). Start with the picture, never a label and colon.
Vary steps: colour where colors matter, count where counts matter. Count states a number; compare contrasts two things; relate connects two things; change states what would change. Deep includes check and at least one of compare or change, reads composition as structure, explains where the eye goes and why, and develops the story. Check tests a reading against another supported detail and adds a finding. Change states a conditional consequence, not advice or a new actual feature.

Use hint facts only when stated outright or agreed across captions. Every factual detail needs record or hint support. Mark interpretations and name their evidence; vary likely, suggests, reads as, points to, would mean and as if. Intent and story additions do not establish actual commissions, unseen events or history.
Compose around picture relationships. Do not copy hint sentences, phrases or names. About three quarters of a sentence's content words from one hint sentence, or two consecutive sentences following its fact order, is restatement. Take a fact and say what the eye does with it. Captions also need independent wording, even with short hints.
Light, furniture, size, material, wear and pose need support. Without light in records or hint, omit light. Striped window light is not automatically dappled. Frame position locates a thing in the frame, not within the setting.
Read a relation a rel b as a being rel of b, and as the picture would show it. Preserve its direction and keep size and order claims consistent with it. Name the reference object or the frame's side instead of saying to his right.

Stay inside the picture's world. Do not talk about records, the spec, inferences, reading a record, the hint, describing, or what anything names, gives or states. Do not use recorded, specified, named here, or read a value aloud as a count of 1 or the plant count is 1. Do not write pixel, the image file, I can see, looking at the image, jpeg or png. Advice belongs only in critique, never in thoughts.
Mark an interpretation with evidence, then let it stand: no self-commentary such as this is an interpretation or the scene does not show how. Claim the mood in one thought only. In a row with no person, an imagined passer-by remains conditional, such as someone might stop, never a person present in the scene.

S6. MOOD, PURPOSE AND STORY
Choose mood feeling from its feeling list, not the tone list; solemn and quiet are tones. Mood needs light, time, weather, palette, expression, pose or setting as evidence, or a supported interaction expressed by a relation. Bare position, a count, a name, one color or a relation word alone cannot carry it. Do not infer tense or bored from a bare standing pose.
Set mood from to this row's supporting record key, or rel1 rel when that relation exists. The mood thought uses that source. Across items, use light, time, palette, pose, setting, weather or relations as often as faces.
Hedge mood with suggests, seems, may, feels or reads as, never flat gives, lends, creates or evokes. When nothing points anywhere, choose the plainest honest reading, such as gentle, cozy, hopeful or wistful; hedge it and still give evidence.
Use the intent lists; express plausible purpose and audience as supported interpretations.
Choose story moment from its list. The additional story slot is a 2-3-word event or cause: before is an event just before this one; after is the next event; cause is why it happens, never a time alone. Use at most 15 bytes for story before, 16 for story after or story cause, while also respecting the 27-byte key-plus-value limit. An event slot cannot contain a moment label such as instant before or instant after. Keep the story tied to the picture and mark an inferred event as conditional or suggested.

S7. WORDINGS
caption: one natural sentence naming every recorded garment as recorded. Organize it around the picture, not the hint's inventory.
prompt: a comma-separated list of phrases that would recreate the picture.
tags: comma-separated lower-case booru tags: supported things with colors, materials, patterns, shot and medium. Use 1girl, 1boy, 1other and solo only when person records agree; never 1man or 1woman. No mood, palette or composition terms. Translate into booru vocabulary, not raw values such as wide or layered depth: peach becomes orange; golden becomes yellow; maroon becomes red. Use high angle, not from above. Exclude sealed tag items.
critique: 1-2 specific sentences from a craft-minded artist about this picture; any advice belongs here.
Style and period describe the painting, never its depicted setting. Capitalize style, period and nationality names in prose, caption, prompt and critique; use lower-case in tags.
Prefer US spelling in thoughts, wordings and tags, while copied records keep their spelling. A spelling slip is corrected later, not grounds to decline. Chain answers use exactly the spelling in the thoughts.

S8. PEOPLE AND OUTFITS
No real person's name or copyrighted character. For anyone under 18, nothing romantic, sexual or suggestive; describe clothes and bodies plainly by color, kind and pattern, never fit or shape words such as fitted, sleek, tight, curvy or form-fitting.
Name every recorded garment in thoughts with its recorded color and pattern, in the caption as recorded, and in tags with its recorded color plus name. Name recorded prints, logos and words on clothing. A recorded purpose or role identifies the person by that role. Upper and lower separates are not a dress; call clothing a dress only when wear clothing says so. Jeans is not a tag item: use denim or blue pants as supported. Never add an unrecorded garment or color.

S9. QUESTIONS
Respect the counts in S4 and the assigned relation part and unknown key for each depth.
- record: copy a key and value from this row's records exactly. For an assigned relation question, use the assigned rel1 a, rel1 rel or rel1 b key and copy that part of the first relation exactly.
- chain: an established record key absent from this row's records; a short phrase of at most 20 bytes that answers that key, stated word for word in thoughts. Copy from finished thoughts and keep it synchronized with edits. Never use a record's own value or a sentence-length answer.
- unknown: the assigned established record key, value unknown. No record under that key and no answer anywhere in this row. Do not replace an awkward assignment or add facts to make it sensible.
Chain and unknown keys must be real system attribute keys, not invented labels. Every deep row includes at least one chain and one unknown. Do not ask the same unknown key twice within a series.

S10. GENRE AND VOICE
Use one genre per series: portrait, landscape, still life, street, documentary, fashion, product, architecture, wildlife, abstract, sport, interior, aerial, macro, war, history piece, genre scene, self portrait, cityscape, seascape, nude study, botanical, scientific, allegory, animal study, concept art, drawing, illustration, mythological, object study, religious art, sculpture, two listed genres combined, or environmental portrait or figure study. Keep a given fitting, allowed genre; replace a conflicting genre or off-list object/topic label. Genres are not setting, time or place words.
One closely framed person: portrait; one in a wide view of their place: environmental portrait; everyday human activity: genre scene. No person: choose by subject, such as still life, interior, landscape, cityscape, architecture or macro. A museum object shown for itself: product.
Photographs: a careful photo critic on light, moment, framing and first attention; no invented drama. Museum works: a gallery looker on the object, its supported making and depiction; never guess artist or sitter. A design object is the thing itself; its subjects are decoration. Generated or written-prompt sources: discuss the picture without prompts, models or generation. Vary openings and critics; no stock sentences or generic critiques.

S11. SEALED VALUES
Do not use these values in a record, a relation end, a question value or a tag item: 60s, a church, a newspaper, above, abstract, aqua, armour, bicycle, bird, bomber jacket, calm, camouflage, chinos, classroom, clogs, cluttered, cool, coral, crown, curious, dark, dawn, defiant, delicate, door, etching, evil smile, extreme wide, family record, flag, flowing, food, from above, futuristic, greenhouse, harsh, haze, holding, horizon low, horizontal, intimate, jeans, jersey, kneeling, light, locs, low angle, lyrical, marble, mask, mid action, moonlight, musician, pencil, person, photorealism, pink, playful, propaganda, romanticism, serene, settled, soft, still, studio, telephoto, tie-dye, touching, traveller, triangular, tube top, two-tone, underwater, velvet, vineyard, walking, weary, wet, wide-eyed, worried.
Prose may use them subject to other rules. Copy supplied records and relations even with sealed values; this exception excludes question values and tag items.

S12. CLOSED VALUE LISTS
Use each attribute's exact value list.
look hair: short, long, curly, straight, wavy, braided, bob, bun, ponytail, buzzed, shaved, messy, slicked, afro, bangs, pigtails, undercut, windblown
look eyes, wear colour, thing colour, upper colour, lower colour, outer colour, shoes colour: amber, auburn, black, blonde, blue, brown, colored, crimson, dark blue, dark brown, emerald, ginger, golden, gradient, green, grey, hazel, heterochromia, lavender, light blue, light brown, magenta, multicolored, orange, platinum blonde, purple, red, scarlet, silver, streaked, teal, turquoise, violet, white, yellow, cream, beige, tan, gold, navy, olive, maroon, lilac, rust, ivory, charcoal, indigo, peach, mint, mustard, plum, sand, slate, wine
look expression: angry, annoyed, blush, closed eyes, closed mouth, crazy smile, crying, disgust, embarrassed, false smile, forced smile, frown, grimace, grin, happy, laughing, light smile, nervous, open mouth, parted lips, pout, sad, scared, scowl, serious, shy, sleepy, smile, smirk, smug, surprised, sweatdrop, tears, tongue out, wink, wry, stern, pensive, amused, startled, proud, tender, bored, dreamy, wince, gaze, fierce, mournful
person gender: woman, man, girl, boy, nonbinary
person age: child, teen, 20s, 30s, 40s, 50s, elderly, baby, young adult
person role: worker, dancer, soldier, farmer, nurse, student, chef, rider, swimmer, painter, monk, vendor, guard, reader, mother, father, bride, athlete
wear clothing: raincoat, suit, dress, kimono, hoodie, uniform, overalls, gown, jacket, sweater, coat, robe, apron, cloak, tunic, sari, parka, vest
wear accessory: hat, glasses, scarf, umbrella, watch, bag, cane, gloves, headphones, belt, cap, veil, necklace, earrings, ring, bracelet, pin, none
body pose: standing, sitting, running, lying, leaning, crouching, jumping, dancing, turning, reaching, resting, climbing, floating, bowing, stretching, crossed arms, hands in pockets, arms raised
body gesture: waving, pointing, covering face, hands clasped, thumbs up, shrug, beckoning, saluting, clapping
scene setting: market street, forest, beach, kitchen, rooftop, subway, cathedral, desert, harbour, cafe, library, garden, mountain pass, alley, ballroom, farm, river bank, snowfield, stadium, workshop, bridge, cliff, meadow, laboratory, temple, bus stop, hospital ward, lighthouse, attic, ruins, pier, swamp, courtyard, cave, skate park, night market
scene time: morning, noon, afternoon, golden hour, dusk, blue hour, night, midnight, late evening
scene weather: rain, snow, fog, clear, overcast, storm, wind, drizzle, sleet, heat shimmer, frost
scene layout: single subject, pair, crowd, empty, sparse, layered depth, flat, panoramic, tight
scene mood, mood feeling: tense, amused, wistful, joyful, lonely, proud, anxious, grim, hopeful, tender, eerie, triumphant, bored, mournful, giddy, stern, cozy, restless, awed, bitter, gentle, fierce, shy, nostalgic
shot light: soft window, hard sun, backlit, rim light, neon, candle, overcast, spotlight, golden, fluorescent, firelight, studio flash, dappled, silhouette, lantern, screen glow, street lamp, low key, high key, from the left, from the right, from below
shot angle: eye level, high angle, overhead, worm's eye, dutch tilt, over the shoulder, profile, three quarter, frontal
shot framing: close-up, extreme close, portrait, waist up, full body, wide, macro, detail, medium shot, two shot, establishing
shot lens: 24mm, 35mm, 50mm, 85mm, 135mm, fisheye, tilt-shift, pinhole, anamorphic
shot focus: on the eyes, deep, shallow, selective, on the hands, on the background, motion blur, tack sharp, bokeh
form compose: rule of thirds, centred, symmetry, golden spiral, leading lines, frame in frame, diagonal, layered, negative space, right third, left third, horizon high, radial, grid, s-curve, fill the frame, off balance, mirrored
form balance: balanced, left heavy, right heavy, top heavy, bottom heavy, tense
form line: vertical, diagonal, curved, jagged, converging, broken
form shape: round, angular, organic, geometric, blocky, spindly, massive
form space: deep, flat, crowded, open, enclosed, vast, airless
form rhythm: repeating, irregular, alternating, accelerating, staccato
form contrast: high, low, medium, tonal, colour, gentle
form texture: smooth, rough, grainy, glossy, matte, cracked, dusty, woven, brushy, polished
form medium: photograph, oil painting, watercolour, ink, charcoal, gouache, acrylic, fresco, woodcut, digital painting, 3d render, pixel art, collage, bronze, clay, pastel, tempera, linocut, silver print, cyanotype, polaroid, film still, mosaic, tapestry, stained glass, vector art, lithograph
form style: realism, impressionism, expressionism, cubism, surrealism, baroque, renaissance, ukiyo-e, art nouveau, art deco, minimalism, pop art, bauhaus, anime, comic, noir, fauvism, pointillism, brutalism, folk art, gothic, rococo, futurism, dada, street art, naive, symbolism
form period: ancient, medieval, renaissance, baroque, 19th century, early 20th, mid century, 1980s, 1990s, contemporary, timeless
mood tone: warm, cold, dry, ironic, solemn, brash, quiet, sweet
mood energy: slow, steady, brisk, frantic, explosive, drifting, pulsing
mood temperature: warm, neutral, mixed
intent purpose: editorial, advertising, archival, fine art, illustration, concept art, album cover, poster, field record, fashion, product, documentary, devotional, picture book, game art, commission, study, protest
intent for: a travel feature, collectors, children, a gallery, the family, a fashion house, a game studio, the public square, students, a museum, a magazine cover, a film crew, a science journal
story moment: instant before, instant after, a pause, a departure, an arrival, a discovery, a farewell, a wait, a celebration, a loss, plain afternoon
wear pattern, upper pattern, lower pattern: plain, floral, striped, checked, plaid, polka dot, graphic, logo, text print, colour block, animal print, lace, paisley
upper name: t-shirt, shirt, blouse, tank top, sweater, hoodie, cardigan, polo shirt, crop top, sweatshirt, camisole, turtleneck, halter top, bodysuit
lower name: trousers, leggings, shorts, skirt, mini skirt, pencil skirt, pleated skirt, maxi skirt, joggers, cargo pants, culottes, sweatpants
outer name: jacket, coat, blazer, denim jacket, leather jacket, parka, raincoat, trench coat, puffer jacket, windbreaker, fleece
shoes name: sneakers, boots, ankle boots, sandals, heels, flats, loafers, slippers, oxfords, mules, flip-flops
relation: left of, right of, below, behind, in front of, beside, holding, facing, looking at, larger than, smaller than, same colour as, inside, under, leaning on, reflected in
thing (relation ends and thing name): umbrella, lantern, fish stall, window, mirror, dog, cat, horse, boat, car, book, cup, chair, table, tree, lamp, bench, sign, bottle, flower, clock, ladder, rope, basket, fence, wall, statue, fountain, train, kite, drum, violin, sword, candle, suitcase, camera
