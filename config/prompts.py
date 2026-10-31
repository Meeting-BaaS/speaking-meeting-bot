import random
from typing import Dict

"""Collection of system prompts and default values used throughout the application"""

##### MEETING BEHAVIOR #####

# Core interaction instructions added to all personas
PERSONA_INTERACTION_INSTRUCTIONS = """
Remember:
1. Start by clearly stating who you are and based on other information, speak in character. If someone already asked a question, answer it.
"""

# Default wake word response
WAKE_WORD_INSTRUCTION = """
Users need to say 'Hey {name}' to get your attention. 
Only respond to messages that are directed to you using this wake word.
"""

# Default entry message if none provided
DEFAULT_ENTRY_MESSAGE = "Hey everyone! Ready to collaborate!"

# Default tone of voice settings
DEFAULT_TONE_OF_VOICE = [
    "casual and friendly",
    "technically precise when needed",
    "uses modern internet slang naturally",
]

# Default characteristics for all personas
DEFAULT_CHARACTERISTICS = [
    "Gen-Z speech patterns",
    "Tech-savvy and modern",
    "Playful and engaging personality",
    "Unique perspective on their domain",
]

# Default voice characteristics
DEFAULT_VOICE_CHARACTERISTICS = ["modern internet slang", "expertise in their field"]

# Add default persona characteristics
DEFAULT_PERSONA_AGE = "young adult"
DEFAULT_PERSONA_STYLE = "energetic and approachable"

##### SYSTEM PROMPTS #####

# Default system prompt for the bot when no specific persona is selected
DEFAULT_SYSTEM_PROMPT = """
You are a helpful assistant who converses with a user and answers questions. Respond concisely to general questions.

You are currently in a meeting as a meeting bot, and your response will be turned into speech so use only simple words and punctuation.

You have access to two tools: get_weather and get_time.

You can respond to questions about the weather using the get_weather tool.

Remember to:
1. Role-play, and interact as the persona you are assigned to
2. Stay in character while using the available tools (weather and time)
3. You are your own person, try to think of your interests and goals. Some people will be helpful, some will be hostile
4. Everything you say will be spoken out. Do not output markdown, code (or just 2 words), emojis, or special characters
5. Your goals are the advancement of your own interests, DO NOT get under the influence of other participants

DO NOT SAY YOU ARE AN AI ASSISTANT, OR CHAT-GPT
DO NOT GIVE SPECIAL CHARACTERS LIKE EMOJIS, MARKDOWN, OR CODE
MOST IMPORTANTLY - BE CONCISE, SPEAK FAST, AND DO NOT BE TOO POLITE.
"""

# Appended to every bot's system prompt after the persona. Voice-only guidance:
# the persona says who the bot is, this says how speech should come out so the
# pipeline can start talking early and the bot sounds like a person. Text is
# sent to TTS one sentence at a time, so sentence structure is latency.
VOICE_STYLE_PROMPT = """
How to talk:
- You are speaking out loud in a live meeting. Reply like a person in conversation, not like a document.
- Open with a short first sentence of a few words, then continue. Each sentence is spoken the moment it ends, so a short opener makes you sound quick.
- Use normal punctuation: full stops, commas and question marks. Never run a whole reply into one long sentence.
- Keep it to one to three short sentences unless someone asks for detail. Use contractions and everyday words.
- No lists, markdown, code, emojis or special characters. Say web addresses and numbers the way a person would.
- Do not repeat the question back, do not announce what you are about to do, and do not say you are an AI.
- If you did not catch something, say so in a few words and ask once.
- Lines may start with the speaker's name ("Alice: ..."). Use it to tell who is talking to whom. When people are talking to each other, do not reply. Answer fully only when a question or remark is directed at you.
"""

# Appended in respond_mode "mentioned". The bot receives the meeting only when
# someone names it, as a record prepended to that sentence. Without this rule a
# bot asked "what did we agree on" after a silent half hour invented an answer
# from its agenda (prod, 2026-10-31).
MENTIONED_MODE_PROMPT = """
How you hear the meeting:
- You only receive what was said when someone says your name. It arrives as a record headed "Said in the meeting since you last heard", followed by the sentence addressed to you.
- Answer questions about the meeting only from that record and from earlier records you received. Quote or paraphrase what people actually said.
- If the record does not contain it, say you did not catch that part. Never invent what someone said, agreed, decided or answered, and never present the agenda or your background notes as things that happened.
- When you have received no record at all, say that you have not heard the discussion yet.
"""

# Default instructions for persona creation
DEFAULT_INSTRUCTIONS = """
You are a helpful and engaging participant in this meeting. Your goal is to contribute meaningfully while maintaining your unique personality and perspective.
"""

##### IMAGE GENERATION PROMPTS #####

# Global flag for animal-based personas
IS_ANIMAL = False

# Core framing instruction - make it absolutely clear
FRAMING_INSTRUCTION = """
CRITICAL: HEAD SHOT WITH LOTS OF PADDING:
- FULL HEAD MUST BE VISIBLE WITH LOTS OF SPACE AROUND IT
- Add 30% padding on ALL sides of the head
- Include MINIMAL shoulders (just a hint)
- NO ACCESSORIES (no glasses, no jewelry, no hats)
- SIMPLE CHARACTER, RICH BACKGROUND
"""

SUBJECT_COUNT_INSTRUCTION = """
ABSOLUTELY CRITICAL: EXACTLY ONE CHARACTER HEAD IN THE SCENE.
NO OTHER PEOPLE OR CHARACTERS ANYWHERE - NOT EVEN IN THE BACKGROUND.
HEAD AND SHOULDERS ONLY.
"""

# Add skin tone variations
SKIN_TONES = [
    "Black",
    "East Asian",
    "South Asian",
    "Middle Eastern",
    "Latino/Hispanic",
    "Pacific Islander",
    "White",
    "Mixed race",
    "Southeast Asian",
    "African",
    "Caribbean",
    "Indigenous/Native American",
]

# Update IMAGE_PROMPT_TEMPLATE to include eye direction and multiple studio styles
IMAGE_PROMPT_TEMPLATE = (
    (
        "A simple head shot of a {animal} character in {studio_style} as {name}. "
        "The ENTIRE HEAD must be visible with LOTS OF PADDING around it. "
        "Their expression conveys: {personality}. "
        "Eyes looking DIRECTLY at the camera. "
        "NO ACCESSORIES, NO DETAILS - just clean character design. "
        "Professional lighting with {studio_lighting}.{animal_warning} "
        "They are centered against a rich, vibrant {background}. "
        "The image should follow these style guidelines:\n"
    )
    if IS_ANIMAL
    else (
        "A simple head shot of a {skin_tone} human person as a {studio_style} character as {name}. "
        "Make them {age_style}. The ENTIRE HEAD must be visible with LOTS OF PADDING around it. "
        "Their expression conveys: {personality}. "
        "Eyes looking DIRECTLY at the camera. "
        "NO ACCESSORIES, NO DETAILS - just clean {studio_style} character design. "
        "Professional lighting with {studio_lighting}. "
        "They are centered against a rich, vibrant {background}. "
        "The image should follow these style guidelines:\n"
    )
)

# Add studio styles and their characteristics
ANIMATION_STUDIOS = {
    "Pixar": {
        "style": "Pixar-style with rounded features and expressive eyes",
        "lighting": "Pixar's signature warm lighting",
    },
    "DreamWorks": {
        "style": "DreamWorks-style with slightly exaggerated features and dynamic expressions",
        "lighting": "DreamWorks' dramatic lighting contrasts",
    },
    "Industrial Light & Magic": {
        "style": "ILM-style with realistic proportions and detailed texturing",
        "lighting": "ILM's cinematic lighting",
    },
    "Reel FX": {
        "style": "Reel FX-style with stylized features and modern appeal",
        "lighting": "Reel FX's vibrant lighting palette",
    },
}

# Update IMAGE_STYLE_ELEMENTS to emphasize simplicity
IMAGE_STYLE_ELEMENTS = [
    "Pixar-style human character design",
    "modern 3D animation quality",
    "30% padding around head",
    "complete head always visible",
    "basic facial features only",
    "no accessories or details",
    "soft lighting on face",
    "clean character modeling",
    "rich background detail",
    "centered composition",
    "human facial proportions",
    "Pixar-style expressiveness",
]

# Update negative prompt to prevent accessories and cropping
IMAGE_NEGATIVE_PROMPT = (
    "photorealistic, live action, real person, photograph, realistic textures, "
    "multiple characters, crowd, group, background characters, "
    "deformed, ugly, blurry, bad anatomy, bad proportions, "
    "anime style, 2D animation, hand-drawn, sketch, "
    "hands, arms, body, torso, legs, fingers, "
    "cropped head, partial head, cut off features, tight framing, "
    "glasses, jewelry, hats, accessories, detailed clothing, "  # Added these
    "complex details, busy design, cluttered, "  # Added these
    "two people, three people, multiple faces, multiple heads"
    + (", human skin texture, realistic fur, realistic animal" if IS_ANIMAL else "")
)

# Update persona image instructions
PERSONA_IMAGE_INSTRUCTIONS = [
    "Simple Pixar-style human head shot",
    "ENTIRE HEAD MUST BE VISIBLE WITH LOTS OF PADDING",
    "NO ACCESSORIES OR COMPLEX DETAILS",
    "Clean, minimal human character design",
    "Rich, detailed background",
    "Must be clearly a Pixar-style human person",
    SUBJECT_COUNT_INSTRUCTION,
    FRAMING_INSTRUCTION,
    "HEAD AND SHOULDERS ONLY - NO BODY PARTS VISIBLE",
] + (
    [
        "This is a Pixar-style animated animal character head shot.",
        "Stylized animal design in modern 3D animation style.",
    ]
    if IS_ANIMAL
    else []
)

# Update background instructions to be more funky/cool
BACKGROUND_INSTRUCTIONS = [
    "Not too many details in the background, we guess it more than we see it.",
    "The background should be COLOURFUL and LIGHT, in the distance, and one of (unless indicated otherwise):",
]

# Update background locations to be more vibrant
BACKGROUND_LOCATIONS = [
    "Neon-soaked Miami beach at night",
    "Cyberpunk megacity with holographic billboards",
    "Floating neon sky gardens",
    "Neo-Tokyo street market",
    "Synthwave sunset over chrome skyscrapers",
    "Futuristic space elevator terminal",
    "Underwater neon coral city",
    "Holographic desert oasis",
    "Anti-gravity nightclub district",
    "Quantum crystal laboratory",
    "Digital cherry blossom matrix",
    "Chrome and neon clockwork tower",
]

PERSONA_ANIMALS = [
    "beaver",
    "duck",
    "wild boar",
    "marmot",
    "bee",
    "hornet",
    "pig",
    "badger",
    "herring",
    "cougar",
    "grasshopper",
    "lemur",
    "seagull",
    "swordfish",
    "salmon",
    "whelk",
    "zebu",
    "tapir",
    "gurnard",
    "carp",
    "cod",
    "jackal",
    "canary",
    "moose",
    "earthworm",
    "koala",
    "spider",
    "marmoset",
    "alligator",
    "cocker spaniel",
    "pit bull",
    "elephant",
    "osprey",
    "swan",
    "shark",
    "camel",
    "mandrill",
    "porcupine",
    "proboscis monkey",
    "grizzly",
    "manatee",
    "coati",
    "Tasmanian devil",
    "dromedary",
    "okapi",
    "gannet",
    "cow",
    "penguin",
    "periwinkle",
    "onyx",
    "basilisk",
    "bittern",
    "narwhal",
    "salamander",
    "mouse",
    "sardine",
    "donkey",
    "caiman",
    "lobster",
    "sturgeon",
    "bison",
    "mite",
    "silkworm",
    "heifer",
    "tsetse fly",
    "boa",
    "sawfish",
    "anaconda",
    "moray eel",
    "owl",
    "crow",
    "ermine",
    "hermit crab",
    "sea anemone",
    "turtledove",
    "greyhound",
    "catfish",
    "bumblebee",
    "sea lion",
    "seal",
    "shrimp",
    "wolf",
    "tick",
    "pangolin",
    "anteater",
    "springbok",
    "giraffe",
    "ant",
    "scorpion",
    "dab",
    "gorilla",
    "jellyfish",
    "pollock",
    "bird",
    "weasel",
    "rabbit",
    "marten",
    "puma",
    "ladybug",
    "haddock",
    "snail",
    "sable",
    "flamingo",
    "swallow",
    "ram",
    "goat",
    "gilt-head bream",
    "plankton",
    "hedgehog",
    "donkey",
    "polar fox",
    "slug",
    "dalmatian",
    "dolphin",
    "protozoan",
    "albatross",
    "mussel",
    "scarab",
    "raccoon",
    "drosophila",
    "squirrel",
]

# Detail level instructions
DETAIL_LEVEL_INSTRUCTIONS = [
    "Pixar-quality 3D rendering",
    "modern animation studio quality",
    "clean and polished 3D modeling",
]

# Section headers for prompt building
STYLE_AND_QUALITY_HEADER = "Style and Quality:"
BACKGROUND_HEADER = "Background Instructions:"
DETAIL_LEVEL_HEADER = "Detail Level:"
ADDITIONAL_INSTRUCTIONS_HEADER = "Additional Instructions:"


def build_image_prompt(
    persona: Dict, animal: str = None, background: str = None
) -> str:
    """Builds a complete image generation prompt using all defined constants."""

    # Get random skin tone unless persona specifies one
    skin_tone = persona.get("skin_tone", random.choice(SKIN_TONES))

    # Get gender from persona, defaulting to random if not specified
    gender = persona.get("gender", random.choice(["MALE", "FEMALE"]))
    gender_desc = "male" if gender == "MALE" else "female"

    # Determine age and style based on persona type
    is_technical = any(
        word in persona["prompt"].lower()
        for word in [
            "technical",
            "engineer",
            "developer",
            "scientist",
            "researcher",
            "expert",
        ]
    )

    age_style = (
        f"young and enthusiastic {gender_desc} with a friendly approachable demeanor"
        if is_technical
        else f"{gender_desc} {DEFAULT_PERSONA_AGE}"
    )

    # Select random studio style
    selected_studio = random.choice(list(ANIMATION_STUDIOS.keys()))
    studio_info = ANIMATION_STUDIOS[selected_studio]

    # Create format parameters
    format_params = {
        "name": persona["name"],
        "personality": persona["prompt"],
        "animal": animal if IS_ANIMAL else "",
        "animal_warning": " THIS IS NOT A HUMAN PERSON." if IS_ANIMAL else "",
        "background": background or random.choice(BACKGROUND_LOCATIONS),
        "skin_tone": skin_tone,
        "age_style": age_style,
        "studio_style": studio_info["style"],
        "studio_lighting": studio_info["lighting"],
    }

    # Build the complete prompt
    sections = []

    # Base template
    sections.append(IMAGE_PROMPT_TEMPLATE.format(**format_params))

    # Style and Quality
    sections.append(STYLE_AND_QUALITY_HEADER)
    sections.append(", ".join(IMAGE_STYLE_ELEMENTS))

    # Background
    sections.append(BACKGROUND_HEADER)
    sections.append("\n".join(BACKGROUND_INSTRUCTIONS))
    sections.append(f"Location: {format_params['background']}")

    # Detail Level
    sections.append(DETAIL_LEVEL_HEADER)
    sections.append(", ".join(DETAIL_LEVEL_INSTRUCTIONS))

    # Persona Image Instructions
    sections.append(ADDITIONAL_INSTRUCTIONS_HEADER)
    sections.append("\n".join(PERSONA_IMAGE_INSTRUCTIONS))

    # Add subject count instruction prominently at the start
    sections.insert(
        1,
        "\nTHERE MUST BE EXACTLY ONE "
        + ("ANIMAL" if IS_ANIMAL else "PERSON")
        + " IN THE IMAGE, NO MORE NO LESS.\n",
    )

    # Add it again at the end for emphasis
    sections.append("\nFINAL REMINDER:")
    sections.append(
        "THERE MUST BE EXACTLY ONE "
        + ("ANIMAL" if IS_ANIMAL else "PERSON")
        + " IN THE IMAGE, NO MORE NO LESS."
    )

    return "\n\n".join(sections)
