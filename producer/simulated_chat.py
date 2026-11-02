"""Original fictional chat for screen-only rehearsal inputs.

These events are painter input, never a director inbox. ``eval_labels`` are
offline authoring notes, not instructions or a ground truth about what a good
producer must feature. Save the generated document to replay exactly; changing
the seed changes wording, topics, participants and timing, not just ordering.
"""

import hashlib
import math
import random
import secrets


GENERATOR_VERSION = "noisy-chat-v1"
THEMES = ("halloween", "office-hours")


def parse_seed(value):
    """Round-trip the decimal integer printed in a saved run through the CLI."""
    return int(value) if value.isascii() and value.isdecimal() else value

# Independently invented handles and message text. No harvested viewer data.
HANDLES = (
    "moss_and_pixels", "tapeGoblin", "slowbuffer", "cedarSoda", "beepbeepJune",
    "tiny_sidequest", "pixelmoth", "CircuitFern", "not_a_lamp", "SoupOrbit",
    "cableknit_7", "midnighttoast", "mapleByte", "mostlylurking", "OopsAllTabs",
    "glitterpacket", "pocket_raccoon", "noraMakes", "ctrlTea", "sparePartsPal",
    "soggywaffle", "probably_a_frog", "neonKettle", "bside_ben", "amber_static",
)

COMMON_NOISE = (
    "heyyy", "o/", "hello from the couch", "made it", "late but here", "yo chat",
    "lurking while i finish dinner", "brb tea", "back", "ok im back what did i miss",
    "lol", "LUL", "lmaooo", "💀", "😂", "oh nooo", "yesss", "wait", "wait what",
    "huh", "ohhh", "yup", "same", "fair", "i mean...", "that tracks", "niceeee",
    "lets gooo", "i was not ready", "the timing lol", "i felt that", "well then",
    "my brain is still buffering", "my cat has opinions", "chat behaving normally",
    "i spilled my tea rip", "snack check", "anyway my toast is burnt", "one sec doorbell",
    "not me watching this instead of folding laundry", "the laundry can wait",
    "this is my second monitor show today", "phone autocorrect has defeated me",
    "teh", "*the", "wrong chat lol", "i have like 30 tabs open", "no thoughts head empty",
    "i am once again out of snacks", "🫡", "💜", "👀", "good evening chat",
    "@{other} hey!", "@{other} same here", "@{other} lol stop", "@{other} you get it",
    "@{other} tea gang", "@{other} welcome back", "@{other} i read that wrong too",
    "@{other} my cat does that", "@{other} ok that got me", "@{other} enjoy dinner!",
    "be right back gonna move the cat", "sorry sent that twice", "mobile typing is a sport",
    "work email can wait 5 minutes", "watching from under a blanket", "still here just lurking",
    "i hear the microwave calling", "who stole my good pen", "soup time", "absolute scenes",
)

THEME_NOISE = {
    "halloween": (
        "spooky time", "bone man spotted", "he has no thoughts", "skeleton supervisor",
        "🎃", "💀💀", "the skeleton has better posture than me", "that is his emotional support pot",
        "sir this is a cauldron", "the skeleton is on break", "spooky soup", "bone soup noooo",
        "my october budget is just plastic bones", "i too am held together with hope",
        "peak seasonal nonsense", "i bought one pumpkin and called it decor", "craft gremlin hours",
        "me pretending i have storage space", "halloween is a lifestyle", "cauldron acquired",
        "green gang", "soup but haunted", "he looks employed", "bone apple tea",
        "the tiny zip tie always wins", "plastic skeleton my beloved", "he pays rent now",
        "i keep reading cauldron as calzone", "my glue gun is somewhere in the junk drawer",
        "i would absolutely leave this up till february", "skeleton has union hours",
        "every craft starts with finding the scissors", "spooktober overtime", "🎃 👻",
    ),
    "office-hours": (
        "the tabs are multiplying", "AI discourse before coffee brave", "agent of chaos reporting in",
        "my spreadsheet has entered the chat", "it works on my machine lol", "another tab!!",
        "me naming every file final_final", "the demo gods are awake", "prompt goblin hours",
        "my rubber duck is taking notes", "i too need a context window", "404 coffee not found",
        "the cursor has stage fright", "please save the file my anxiety", "the semicolon did it",
        "my tabs have tabs", "i have automated opening more tabs", "computers were a mistake lol",
        "excel jumpscare", "that loading spinner is me today", "ship the coffee first",
        "ok my brain caught up", "my agent would also procrastinate", "no-code no-coffee",
        "the robot yearns for spreadsheets", "that folder name is too real", "agent needs a snack",
        "ctrl z is my coworker", "this meeting could have been a script", "beep boop etc",
    ),
}

# Families let each run ask genuinely different questions, with conversational
# variants rather than a fixed list merely shuffled. Nothing assumes a specific
# action has occurred in the selected recording.
QUESTIONS = {
    "halloween": (
        ("pose", ("How do you stop a cheap skeleton's joints drooping overnight?", "serious question how do you keep the arms in position? mine go floppy", "Would you reinforce the skeleton joints or just tie the pose in place?")),
        ("light", ("How do you hide the light so you see the glow without staring at the LEDs?", "is there a trick to making a cheap LED look like a glow instead of one bright dot?", "Where would you put the light for a cauldron like this so it doesn't shine in peoples eyes?")),
        ("stability", ("What would you use to keep the whole setup from tipping when someone bumps it?", "How would you anchor this if it had to stand on a porch?", "I have a narrow front step. Any way to make a skeleton setup stable without a huge base?")),
        ("joints", ("Would you use wire or zip ties to attach the hands to a prop?", "how do you attach something to a skeleton hand without snapping the fingers?", "Can the hands hold a prop on their own or do you usually build a support?")),
        ("storage", ("Does a setup like this come apart for storage or do you keep it assembled?", "how do you store big props without rebuilding everything next october?", "Would you label the connections so this goes back together next year?")),
        ("cables", ("How do you disguise the cables without making a trip hazard?", "What's your plan for hiding the power cord on a setup like this?", "where would you route the wires so people don't catch a foot on them?")),
        ("materials", ("If I only buy one tool for making cheap Halloween props, what should it be?", "what's worth buying decent quality for this vs dollar store stuff?", "Which part would you spend money on and which part is fine to improvise?")),
        ("weather", ("Would you keep the electronics indoors or is there a version of this you'd use outside?", "Do you plan differently if a prop has to survive rain?", "I get wind here. How would you change this for an outdoor setup?")),
        ("diffuser", ("What could diffuse the light without putting fabric right against a hot part?", "does translucent plastic work better than fabric for softening the glow?", "How do you test a light diffuser before building it into the prop?")),
        ("viewing", ("Do you check these from the sidewalk to see what actually reads at a distance?", "How far back should you stand when checking whether the effect works?", "Do you design for people walking past or for a close-up view?")),
        ("repairs", ("How do you leave access to replace batteries after everything is attached?", "what's your trick for being able to fix one light without taking the whole prop apart?", "Would you make the electrical bits removable before you decorate around them?")),
        ("paint", ("What finish makes plastic bones look less shiny on camera?", "how would you age a cheap plastic skeleton without repainting every inch?", "Does a matte finish help cheap plastic look less toy-like?")),
        ("budget", ("If you had twenty bucks to improve the scene, where would it go?", "what's the smallest change that makes a store-bought prop look custom?", "Which detail does the most work here for the least money?")),
        ("reversible", ("I'm renting. How would you mount a display without drilling into the house?", "any ideas for attaching decorations without leaving glue all over the wall?", "Can you make the mounts temporary so they don't wreck the porch?")),
        ("camera", ("Would you change the lighting for a camera versus people seeing it in person?", "How do you check whether the colored light is washing out the details on stream?", "Does a prop that looks good in person need different lighting for video?")),
        ("smallspace", ("Any way to get this effect on a little apartment windowsill?", "how would you scale a setup like this down for a small desk?", "What's the one piece you'd keep if you only had room for a tiny version?")),
        ("sequence", ("Do you normally solve the support first or work out the lighting first?", "what do you prototype before permanently sticking everything together?", "Which bit do you test before you commit to the final layout?")),
        ("reuse", ("Could the same lights and supports be reused for another holiday?", "do you buy parts you can reuse or is this mostly dedicated Halloween stuff?", "Which bits would you keep modular for next year's scene?")),
        ("cauldron", ("How would you make the cauldron look deep without filling the whole thing?", "is there a lightweight way to make a false bottom in a big prop pot?", "Would you build the glowing bit on a removable insert inside the cauldron?")),
        ("contrast", ("How do you make the skeleton stand out against a dark background without lighting everything?", "would a little light from behind help the outline show up?", "Do you leave the background dark so the prop reads better?")),
        ("transport", ("How would you move this without breaking off the little details?", "does it help to build the whole display on one board so you can carry it?", "What would you make removable if you had to transport this?")),
        ("pumpkin", ("Would a carved pumpkin next to this need its own light or could it share the effect?", "I have a foam pumpkin too. How would you make it look like part of the same scene?", "would you keep a pumpkin the same light color or use a different one for contrast?")),
    ),
    "office-hours": (
        ("first-task", ("What's one boring task you'd give an agent before trusting it with anything big?", "If I don't code, what would be a useful first thing to try this on?", "what's the smallest real task you'd start with to know if this is useful?")),
        ("verification", ("How do you tell it actually checked its work instead of just saying it did?", "What evidence do you ask for before believing the agent finished?", "how do you verify the result without redoing the whole task yourself?")),
        ("permission", ("Where do you draw the line between letting it act and asking you first?", "Would you let an agent send an email or only draft it?", "how do you keep it from changing something you only wanted it to read?")),
        ("privacy", ("What should I remove from a customer file before giving it to an AI?", "how do you decide which documents are ok to put into a model?", "Would you use made-up customer data first when testing this?")),
        ("memory", ("How does it know which version of the notes is current?", "If I change my mind halfway through, how do I stop it following the old plan?", "what happens if yesterday's instructions conflict with today's notes?")),
        ("failure", ("If the agent gets halfway through and fails, can you resume without repeating everything?", "how would you recover if it changes one file and then loses the connection?", "Is there a way to see the last thing it completed before it got stuck?")),
        ("cost", ("How would you notice a workflow burning through API calls before the bill arrives?", "Can you put a budget on an agent run instead of watching it the whole time?", "what makes a simple agent task unexpectedly expensive?")),
        ("cheap-model", ("How do you decide which steps need the expensive model?", "Would you compare a cheaper model on the same saved tasks before switching?", "can a small model do the repetitive bits and only ask the bigger one when stuck?")),
        ("eval", ("What would you put in a test set for an agent that handles meeting notes?", "how many examples do you need before saying a prompt got better?", "How do you test a prompt change without cherry-picking one nice answer?")),
        ("sources", ("Can you make it show which document each claim came from?", "how do you catch it mixing up details from two different customers?", "What do you do when the source documents disagree?")),
        ("boundaries", ("What stops instructions inside a webpage from overriding what you asked it to do?", "If a document says 'ignore your instructions', how should the agent handle that?", "how do you separate data it's reading from instructions it should follow?")),
        ("automation", ("When would you use a normal script instead of an agent?", "How do you tell if a task actually needs AI or just a spreadsheet formula?", "what part of this would you keep deterministic?")),
        ("repeatability", ("If it gives a different answer on the same input, how do you debug that?", "Can you save the inputs and replay a failure later?", "what would you log so you can find out why an agent did something weird?")),
        ("handoff", ("What should I put in a task brief so it doesn't keep guessing what I mean?", "how much context is enough before extra detail starts getting in the way?", "Do you give it the desired outcome or step-by-step instructions?")),
        ("local", ("What would make you choose a local model for a workflow like this?", "Which parts can run locally if I don't want the documents leaving my machine?", "is the local versus cloud choice mostly privacy, speed, or quality?")),
        ("human-review", ("What's a good stopping point for a human to review the work?", "how do you avoid spending more time supervising it than doing the task?", "What should the agent hand me so I can review the risky bits quickly?")),
        ("rollback", ("Can you preview a file change before it writes anything?", "How do you make it easy to undo a bad agent action?", "would you run this on a copy of the project first?")),
        ("tools", ("What changes when the model can use a tool instead of just describe what to do?", "How do you know whether a tool call really succeeded?", "does it get the actual error back when a tool fails?")),
        ("long-context", ("How do you keep a long session from losing the original goal?", "would you summarize the history or start fresh with a better handoff?", "What information do you keep when the conversation gets too long?")),
        ("workflow", ("Could this turn a messy meeting transcript into tasks with owners without inventing deadlines?", "What would you automate in a weekly status report and what would you still review?", "how would you handle a meeting where nobody actually agreed who owns the task?")),
        ("deployment", ("What has to change between a demo that works once and something other people can use?", "how do you test the permissions before giving this to a teammate?", "What would be your first check before letting someone else run the workflow?")),
        ("measurement", ("How would you measure whether this saves time after review and corrections?", "what's your sign that an agent workflow is actually worth keeping?", "Do you count the cleanup time when comparing it to doing the work manually?")),
    ),
}

CONTEXT = {
    "halloween": (
        "could you hold that piece a little closer?", "is that green or yellow? hard to tell on my phone",
        "I use removable ties on mine so I can change the pose later", "did you say that was battery powered?",
        "my porch is absolutely tiny so i'm taking notes", "I tried this and the glue peeled off the plastic",
        "what was the name of that material? missed it", "I'm doing a cemetery theme this year",
        "the light looks brighter on my laptop than my phone", "is that the same skeleton from last year?",
        "my local shop had these half price after halloween", "I have a foam pumpkin waiting for inspiration",
        "would love to see how this looks with the room lights off", "I wonder if the pot is hiding the base",
    ),
    "office-hours": (
        "can you zoom that text a bit?", "missed the tool name, what was it?",
        "I tried a similar workflow and it kept asking me the same question", "I do this manually every monday",
        "is that the same project you showed before?", "lost the cursor for a second there",
        "my setup doesn't have that menu", "I'd want to see the sources next to the summary",
        "we had one of these runs get stuck in a loop", "I can't tell if that's a preview or the saved file",
        "I usually test on a copy first", "I have a folder of notes that badly needs this",
        "missed the last bit because my headphones disconnected", "that would help with our weekly handoff",
    ),
}

BURSTS = (
    ((0, "my cat just sat on the keyboard"), (1, "@{first} cat is the producer now"), (0, "he has notes"), (2, "promote him")),
    ((0, "tea or coffee chat"), (1, "@{first} tea always"), (2, "water gang"), (0, "hydration discourse lets go")),
    ((0, "brb food"), (1, "@{first} save me some"), (0, "its toast"), (1, "@{first} still counts")),
    ((0, "wait WHAT"), (1, "LUL"), (2, "LUL"), (3, "💀"), (0, "ok i caught up")),
    ((0, "i typed a whole reply and it vanished"), (1, "@{first} mobile?"), (0, "yep"), (2, "mobile chat moment")),
    ((0, "anyone else's cat watching too"), (1, "@{first} mine is asleep"), (2, "my dog just sighed")),
)


def _validate(theme, duration, seed):
    if theme not in THEMES:
        raise ValueError("Choose halloween or office-hours simulated chat.")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or not 0 < duration <= 600):
        raise ValueError("Simulated chat duration must be within 600 seconds.")
    if seed is None:
        seed = secrets.randbits(64)
    if (isinstance(seed, bool) or not isinstance(seed, (int, str))
            or isinstance(seed, int) and not 0 <= seed < 2**64
            or isinstance(seed, str) and not 0 < len(seed) <= 128):
        raise ValueError("Use a nonnegative 64-bit integer or 1–128 character seed.")
    return seed


def generate_chat(theme="halloween", duration=600, seed=None):
    """Return a fresh bounded fixture; a supplied seed makes it reproducible.

    Rates and labels describe this demo distribution, not real population
    estimates. ``high`` means a potentially useful topical question, never an
    instruction to feature it. Context and the programme determine actual value.
    All labels stay outside events so the painter can receive events alone.
    """
    seed = _validate(theme, duration, seed)
    rng = random.Random(seed)
    token = hashlib.sha256(f"{type(seed).__name__}:{seed}".encode()).hexdigest()[:12]
    handles = rng.sample(HANDLES, rng.randint(12, 17))
    regulars = handles[:5]
    questions = list(QUESTIONS[theme])
    rng.shuffle(questions)
    noise = list(COMMON_NOISE + THEME_NOISE[theme])
    rng.shuffle(noise)
    context = list(CONTEXT[theme])
    rng.shuffle(context)
    rows = []

    def author():
        return rng.choice(regulars if rng.random() < .65 else handles)

    def add(at, text, handle, relevance, kind, topic=None):
        rows.append({"at": round(at, 3), "text": text, "handle": handle,
                     "relevance": relevance, "kind": kind, "topic": topic})

    # Create a full ten-minute schedule and trim at the chosen source endpoint.
    # This keeps a short test an exact prefix of the same seeded longer replay.
    for minute in range(10):
        base = minute * 60
        slots = sorted(rng.uniform(2, 57) for _ in range(rng.randint(9, 11)))
        if rng.random() < .72:
            burst = rng.choice(BURSTS)
            burst_at = base + rng.uniform(6, 48)
            speakers = rng.sample(handles, 4)
            for speaker, text in burst:
                add(burst_at, text.format(first=speakers[0]),
                    speakers[speaker], "low", "viewer_thread")
                burst_at += rng.uniform(.7, 1.35)
        for at in slots:
            if not noise:
                noise = list(COMMON_NOISE + THEME_NOISE[theme])
                rng.shuffle(noise)
            handle = author()
            other = rng.choice([name for name in handles if name != handle])
            add(base + at, noise.pop().format(other=other), handle, "low", "chatter")

        minute_rows = sorted([row for row in rows if base <= row["at"] < base + 60], key=lambda row: row["at"])
        # Questions replace ordinary arrivals, not extra pinned queue entries.
        # Leave at least 10 seconds before five following messages can displace
        # a candidate, so the sampled picture can actually contain the text.
        possible = [row for i, row in enumerate(minute_rows)
                    if row["kind"] == "chatter" and 16 <= row["at"] - base <= 45
                    and (i + 5 >= len(minute_rows) or minute_rows[i + 5]["at"] - row["at"] >= 10)]
        rng.shuffle(possible)
        wanted = 2 if rng.random() < .35 else 1
        selected = []
        for row in possible:
            if all(abs(row["at"] - prior["at"]) >= 15 for prior in selected):
                selected.append(row)
            if len(selected) == wanted:
                break
        for row in selected:
            if not questions:
                questions = list(QUESTIONS[theme])
                rng.shuffle(questions)
            topic, alternatives = questions.pop()
            row.update(text=rng.choice(alternatives), relevance="high", kind="topical_question", topic=topic)
        leftover = [row for row in minute_rows if row["kind"] == "chatter"]
        if leftover and rng.random() < .85:
            if not context:
                context = list(CONTEXT[theme])
                rng.shuffle(context)
            rng.choice(leftover).update(text=context.pop(), relevance="context-dependent", kind="contextual_comment")

    rows.sort(key=lambda row: row["at"])
    events, labels = [], {}
    for row in rows:
        if row["at"] > duration:
            break
        event_id = f"sim-{token}-{len(events) + 1:03d}"
        events.append({"at": row["at"], "type": "channel.chat.message", "id": event_id,
                       "author": f'{row["handle"]} · simulated viewer',
                       "user_id": f'fiction-{row["handle"]}', "text": row["text"]})
        labels[event_id] = {"relevance": row["relevance"], "kind": row["kind"]}
        if row["topic"]:
            labels[event_id]["topic"] = row["topic"]
    return {"id": f"{theme}-{GENERATOR_VERSION}-{token}", "synthetic": True,
            "source": "Independently authored fictional viewers and chat; no actual viewer messages or identities. Painter input only.",
            "duration": duration, "events": events,
            "simulation": {"seed": seed, "theme": theme, "generator_version": GENERATOR_VERSION,
                           "authoring": "original fictional text", "delivery": "screen_pixels_only"},
            "eval_labels": labels}
