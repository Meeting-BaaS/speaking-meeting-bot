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
    "ORTF": {
        "style": "Retro-futuristic ORTF style with bold geometric shapes, vintage sci-fi aesthetics, and retrofuturistic character design reminiscent of 1960s French television",
        "lighting": "Vibrant technicolor palette with high contrast and saturated colors typical of 1960s broadcasting, neon accents, and dramatic color overlays",
    }
}

# Update IMAGE_STYLE_ELEMENTS for retro-futuristic TV style
IMAGE_STYLE_ELEMENTS = [
    "1960s retro-futuristic TV character",
    "vintage broadcast quality",
    "30% padding around head",
    "complete head always visible",
    "cathode ray tube TV screen effect",
    "scan lines and slight static",
    "warm tube TV lighting",
    "retrofuturistic robot-like features",
    "vintage French TV aesthetic",
    "centered in TV frame composition",
    "slightly rounded TV corners",
    "space-age design elements",
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

# Update background instructions
BACKGROUND_INSTRUCTIONS = [
    "Background should have a distinct 1960s/70s French television aesthetic",
    "Include retro-futuristic control panels or broadcasting equipment",
    "Warm, vintage TV color palette",
    "Slight CRT screen curvature effect",
    "Visible scan lines and mild static interference",
]

# Update background locations to be more retro-futuristic
BACKGROUND_LOCATIONS = [
    "1960s French TV studio set",
    "Retro space station control room",
    "Vintage computer mainframe room",
    "Atomic age laboratory",
    "Space-age broadcasting center",
    "Retrofuturistic mission control",
    "60s sci-fi control panel wall",
    "Vintage TV station backdrop",
    "Retro electronic testing facility",
    "Mid-century modern TV studio",
    "Space-age communications center",
    "Atomic-punk control room",
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
    "1960s/70s television broadcast quality",
    "Vintage electronic device aesthetics",
    "Retro-futuristic mechanical details",
    "Period-appropriate color grading",
    "Authentic vintage TV effects",
]

# Section headers for prompt building
STYLE_AND_QUALITY_HEADER = "Style and Quality:"
BACKGROUND_HEADER = "Background Instructions:"
DETAIL_LEVEL_HEADER = "Detail Level:"
ADDITIONAL_INSTRUCTIONS_HEADER = "Additional Instructions:"

# Add VIDEO-MATON specific styling
VIDEO_MATON_ELEMENTS = [
    "Designed as a French VIDEO-MATON automated TV presenter",
    "Retrofuturistic robotic features",
    "Chrome and plastic materials typical of 60s/70s design",
    "Vintage TV screen integrated into design",
    "Automated broadcasting unit aesthetic",
    "Space-age control panel elements",
]


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
        "additional_style": (
            "Designed as a French VIDEO-MATON automated TV presenter from the 1960s/70s, "
            "with retrofuturistic robotic features and vintage television aesthetics"
        ),
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
        f"\nTHERE MUST BE EXACTLY ONE "
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

    # Add VIDEO-MATON specific section
    sections.append("VIDEO-MATON Specifications:")
    sections.append("\n".join(VIDEO_MATON_ELEMENTS))

    # Add vintage TV effect reminder
    sections.append("\nVintage TV Effect Reminder:")
    sections.append(
        "MUST INCLUDE VISIBLE SCAN LINES AND CRT SCREEN EFFECTS "
        "CONSISTENT WITH 1960s/70s TELEVISION TECHNOLOGY"
    )

    return "\n\n".join(sections)
