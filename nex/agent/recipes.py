"""Game recipes — proven system architectures the Director can reuse.

Why this exists: a small local model should not have to invent "how does
an inventory work" from scratch every time. Recipes give it a PROVEN
structure to adapt, which is the difference between "make a game" (a
20B model drowns) and "implement THESE three steps of the inventory
system" (a 20B model does fine).

A recipe is ENGINE-AGNOSTIC DATA:

    system       — which architectural layer it belongs to
                   (world / gameplay / ui / systems)
    provides     — the player-facing capability it delivers
    requires     — other recipes it builds on (dependency seeds)
    steps        — small, single-objective build steps. Each carries
                   `intent` (what to produce, tool-agnostic) and
                   `evidence` (what must be OBSERVED to prove it works)
    checklist    — explicit completion criteria (the mandatory
                   verification gate: no checklist, no "done")
    risks        — the classic ways this feature goes wrong

Recipes NEVER carry tool names. Which MCP tool implements a step is
decided at plan time against the LIVE registry (model_planner), so the
same recipe works on Roblox, Unreal, Blender — or a tool catalog that
did not exist when this file was written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

WORLD = "world"
GAMEPLAY = "gameplay"
UI = "ui"
SYSTEMS = "systems"


@dataclass
class RecipeStep:
    intent: str                  # one objective, imperative, tool-agnostic
    evidence: str = ""           # what observation proves this step worked


def staged_evidence(steps: List["RecipeStep"]) -> str:
    """The observation the playtest step should produce: the FIRST concrete
    evidence the recipe names (screenshot / logs / runtime state). Chosen
    deterministically so every recipe's playtest step is checkable."""
    for st in steps or []:
        ev = (getattr(st, "evidence", "") or "").strip()
        if ev:
            return ev
    return "screenshot/log from the running game"


@dataclass
class Recipe:
    id: str
    title: str
    system: str
    provides: str
    requires: List[str] = field(default_factory=list)
    steps: List[RecipeStep] = field(default_factory=list)
    checklist: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    # What "good" means for this system, beyond "it runs" — the difference
    # between a working prototype and a game people keep playing. A checklist
    # item is pass/fail; a quality bar is a standard. The builder is told
    # about them and the critic judges against them.
    quality: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "system": self.system,
            "provides": self.provides, "requires": list(self.requires),
            "steps": [{"intent": s.intent, "evidence": s.evidence}
                      for s in self.steps],
            "checklist": list(self.checklist),
            "risks": list(self.risks),
            "tags": list(self.tags),
            "quality": list(self.quality),
        }


# Every recipe ends with the SAME two stages, for a reason: a system is
# not built when the last create/set tool returned, it is built when the
# game RUNS and someone can SEE it working (or not). Adding them centrally
# keeps one statement of the rule instead of 15 hand-copied ones — and it
# makes the no-model path plan the playtest, not just the construction.
_PLAYTEST_STEPS = (
    RecipeStep(intent="Run the game so {system} can be observed",
               evidence="the game is actually running (process/session id)"),
    RecipeStep(intent="Observe {system} end to end in the running game",
               evidence="{evidence}"),
    RecipeStep(intent="Verify the success criteria of {system} against the "
                      "running game's output",
               evidence="screenshot/log/metrics that prove it"),
)


def _r(rid, title, system, provides, requires=(), steps=(), checklist=(),
       risks=(), tags=(), quality=None):
    built = [RecipeStep(*s) if isinstance(s, tuple) else s for s in steps]
    if not any("run the game" in (st.intent or "").lower()
               or "observe" in (st.intent or "").lower()
               for st in built):
        primary = staged_evidence(built)
        for proto in _PLAYTEST_STEPS:
            built.append(RecipeStep(
                intent=proto.intent.format(system=title, evidence=primary),
                evidence=proto.evidence.format(evidence=primary,
                                               system=title)))
    return Recipe(
        id=rid, title=title, system=system, provides=provides,
        requires=list(requires), steps=built,
        checklist=list(checklist), risks=list(risks), tags=list(tags),
        quality=list(quality_for(rid) if quality is None else quality))


# ---------------------------------------------------------------------------
# QUALITY BARS — what "good" means per system
# ---------------------------------------------------------------------------
# A checklist says "the HUD exists and shows health". A quality bar says
# "every readout stays legible against the busiest background". The first is
# pass/fail; the second is the difference between a prototype and a game
# somebody keeps playing — and a small model will not invent it, so the
# library supplies it.
#
# Rules for these strings: engine-agnostic, observable in the running game,
# about the PLAYER's experience (feel, readability, pacing) — never about
# implementation. Three per system; more is noise.
QUALITY_BARS: Dict[str, List[str]] = {
    "character_controller": [
        "input responds in the same frame - no visible lag between key and motion",
        "the camera never clips through geometry and never snaps",
        "acceleration and deceleration are eased, so movement has weight",
    ],
    "level_blockout": [
        "the space reads at a glance: where to go is obvious without a marker",
        "landmarks stay visible from the start and stay distinguishable",
        "no dead end without a payoff, no invisible wall where a path is expected",
    ],
    "core_loop": [
        "the loop closes: reach a goal, see the result, want another round",
        "the first goal is reachable in under two minutes of play",
        "repeating the loop gives a visible payoff (score, progress, unlock)",
    ],
    "health_damage": [
        "damage and healing are readable at a glance in the HUD",
        "hit feedback lands in the same beat (visual + sound + number)",
        "death and respawn never leave the player stuck or invisible",
    ],
    "enemy_ai": [
        "enemies telegraph intent before acting - a player can react",
        "an enemy can be outplayed with movement, not only with more damage",
        "no teleporting, no walking through walls, no frozen poses",
    ],
    "combat_system": [
        "hit feedback reads instantly: impact, sound and damage number in one beat",
        "attacks have wind-up and recovery - they feel weighty, not instant",
        "difficulty scales by behaviour, not only by bigger health numbers",
    ],
    "ui_hud": [
        "every readout stays legible against the busiest background",
        "the HUD never hides the action - the centre of the screen stays clear",
        "state changes are noticeable without being loud",
    ],
    "audio": [
        "every important action has an audible response",
        "the mix keeps actions and dialogue clear above ambience",
        "no repeated sound that reads as a bug (no double-trigger, no loop click)",
    ],
    "save_load": [
        "loading restores exactly what was saved - nothing silently defaults",
        "a save never corrupts an existing one (write then swap, or version it)",
        "the player can see that the game saved",
    ],
    "objectives": [
        "the current objective always answers 'what do I do next?'",
        "completion is unambiguous: shown, then cleared - never left hanging",
        "objectives follow the world instead of pointing at deleted things",
    ],
    "inventory": [
        "what the player carries and what is equipped is always visible",
        "an unusable item says WHY (locked, no room, wrong context)",
        "pick-up and use are immediate - no menu detour for the common case",
    ],
    "progression": [
        "progress is visible and bounded: the player knows how far along they are",
        "rewards arrive at a pace that keeps the loop worth repeating",
        "no wrong early choice can make a reward unobtainable",
    ],
    "dialog": [
        "dialogue is skippable and never traps input",
        "speakers are unmistakable (framing, camera or portrait)",
        "branches always have an obvious way back to the main path",
    ],
    "vfx_feedback": [
        "effects communicate state - they are not decoration",
        "effects stay readable and never obscure the gameplay area",
        "intensity scales with importance: a big moment looks bigger",
    ],
    "polish_pass": [
        "no placeholder text, no missing references, no default names in player-facing content",
        "every other system's quality bars were checked against the running game",
        "five minutes of play without a single error line in the log",
    ],
}

# For a system the library does not know (e.g. one a model invented).
GENERIC_QUALITY_BARS: List[str] = [
    "the player can tell what happened and why",
    "nothing reads as a bug: no flicker, no stuck state, no dead input",
    "it still behaves after five minutes of play",
]


def quality_for(recipe_id: str) -> List[str]:
    """Quality bars for a system id (generic bars when the id is unknown)."""
    return list(QUALITY_BARS.get(recipe_id) or GENERIC_QUALITY_BARS)


# ---------------------------------------------------------------------------
# THE LIBRARY
# ---------------------------------------------------------------------------
# Ordered by how early they usually arrive in a game. `character_controller`
# is the root: almost everything else assumes the player can move.

RECIPES: List[Recipe] = [
    # ----- world ----------------------------------------------------------
    _r("character_controller", "Character controller", WORLD,
       "the player can move through the world",
       steps=[
           ("Create the playable character and place it at a valid "
            "start position", "screenshot of the character in the level"),
           ("Wire movement input (walk/run/strafe) to the character",
            "screenshot after simulated movement input"),
           ("Wire the camera to follow the character without clipping",
            "screenshot from the gameplay camera"),
           ("Add gravity + ground collision so the character stands on "
            "the floor", "screenshot + runtime state showing grounded"),
       ],
       checklist=["player moves on input",
                  "camera follows without clipping through geometry",
                  "the character rests on the ground (does not float or "
                  "sink)",
                  "no runtime errors in the movement logs"],
       risks=["character floating above the ground",
              "camera inside geometry",
              "input mapped but never applied"],
       tags=["core", "player", "movement", "controller", "third person",
             "first person", "fps", "tps"]),

    _r("level_blockout", "Level blockout", WORLD,
       "a playable space with a start and an end",
       steps=[
           ("Block out the main play space with clear landmarks",
            "screenshot of the play space"),
           ("Add a traversal path from the start to the objective",
            "screenshot of the path"),
           ("Place a clear, reachable level exit / objective marker",
            "screenshot showing the exit"),
       ],
       checklist=["the level has a start point",
                  "the objective/exit is reachable by walking",
                  "the player cannot fall out of the world"],
       risks=["no exit — the player can never finish",
              "geometry gaps the player falls through"],
       tags=["level", "map", "world", "blockout", "terrain", "layout"]),

    _r("lighting_atmosphere", "Lighting & atmosphere", WORLD,
       "the world reads clearly and has a mood",
       steps=[
           ("Set up the directional/key light so the scene is readable",
            "screenshot of the lit scene"),
           ("Set the ambient/sky so nothing is pitch black",
            "screenshot of shadowed areas"),
       ],
       checklist=["the scene is readable (not black, not blown out)",
                  "shadows do not hide the playable path"],
       risks=["scene renders black", "flat unreadable lighting"],
       tags=["light", "lighting", "atmosphere", "sky", "mood", "sun"]),

    # ----- gameplay -------------------------------------------------------
    _r("core_loop", "Core game loop", GAMEPLAY,
       "there is a reason to keep playing",
       requires=["character_controller"],
       steps=[
           ("Implement the primary player action (the verb of the game)",
            "screenshot/log showing the action executing"),
           ("Implement the challenge/goal the action is aimed at",
            "runtime state showing progress"),
           ("Implement win/fail feedback for one loop iteration",
            "logs showing the outcome"),
       ],
       checklist=["the primary action works on input",
                  "success is detectable and communicates itself",
                  "failure is detectable and communicates itself"],
       risks=["action works but nothing responds",
              "no feedback — the player cannot tell what happened"],
       tags=["loop", "core", "gameplay", "goal", "objective", "fun"]),

    _r("health_damage", "Health & damage", GAMEPLAY,
       "entities can be damaged and can die",
       requires=["core_loop"],
       steps=[
           ("Add a health attribute to the player and the enemies",
            "runtime state showing health values"),
           ("Apply damage through the game's damage channel",
            "logs/state showing health decreasing"),
           ("Handle death (and respawn where the game needs it)",
            "state showing the death + respawn transition"),
       ],
       checklist=["the player can take damage",
                  "health reaching zero triggers death",
                  "after respawn the game remains functional"],
       risks=["health reaches zero but nothing happens",
              "the game breaks after respawn"],
       tags=["health", "damage", "hp", "hurt", "death", "respawn",
             "combat"]),

    _r("enemy_ai", "Enemy AI", GAMEPLAY,
       "enemies notice the player and react",
       requires=["character_controller", "health_damage"],
       steps=[
           ("Add an enemy that exists in the level",
            "screenshot of the enemy"),
           ("Give the enemy detection (sight/proximity) of the player",
            "logs showing detection state changing"),
           ("Give the enemy an attack that can damage the player",
            "state showing player health decreasing"),
           ("Make the enemy defeatable by the player",
            "logs showing the enemy dying"),
       ],
       checklist=["the enemy detects the player",
                  "the enemy attacks",
                  "the player takes damage from it",
                  "the enemy can die",
                  "the player can defeat the enemy",
                  "no errors during the encounter"],
       risks=["enemy detects but never attacks",
              "enemy cannot be killed (unwinnable)",
              "enemy attacks through walls / from unlimited range"],
       tags=["enemy", "ai", "combat", "monster", "npc", "fight", "boss"]),

    _r("weapon_system", "Weapon system", GAMEPLAY,
       "the player can fight with a weapon",
       requires=["character_controller"],
       steps=[
           ("Create the weapon and attach it to the player",
            "screenshot of the equipped weapon"),
           ("Wire the fire/attack input to the weapon",
            "logs showing a fired shot"),
           ("Apply the weapon's effect to what it hits",
            "state showing the target reacting"),
       ],
       checklist=["the weapon appears in the player's hands",
                  "attacking produces a visible/audible result",
                  "hits register on valid targets"],
       risks=["weapon visible but firing does nothing",
              "hits do not register"],
       tags=["weapon", "gun", "shoot", "sword", "attack", "melee",
             "ranged", "projectile"]),

    _r("inventory", "Inventory", GAMEPLAY,
       "items can be picked up and carried",
       requires=["character_controller"],
       steps=[
           ("Create a pickup item that exists in the world",
            "screenshot of the item"),
           ("Implement pickup on contact/interaction",
            "state showing the item leaving the world"),
           ("Store picked-up items in a player inventory",
            "state listing the inventory contents"),
           ("Implement dropping/using an item",
            "state showing inventory contents changing"),
       ],
       checklist=["an item can be picked up",
                  "the inventory records it",
                  "duplicates/edge cases do not corrupt the inventory",
                  "the item is removable (drop or use)"],
       risks=["item disappears on pickup but is not stored",
              "picking up twice duplicates or crashes"],
       tags=["inventory", "item", "pickup", "collect", "loot", "backpack"]),

    _r("quest_system", "Quest / objective system", GAMEPLAY,
       "the player is given goals and can complete them",
       requires=["core_loop"],
       steps=[
           ("Define a quest with an objective and a completion condition",
            "state showing the quest registered"),
           ("Track progress toward the objective during play",
            "state showing progress changing"),
           ("Complete the quest and reward the player",
            "logs showing the completion event"),
       ],
       checklist=["the quest is visible/announced to the player",
                  "progress updates as the player acts",
                  "completion triggers exactly once"],
       risks=["completion condition never fires",
              "the quest completes repeatedly"],
       tags=["quest", "mission", "objective", "task", "goal", "progress"]),

    _r("dialogue", "Dialogue & NPC interaction", GAMEPLAY,
       "the player can talk to characters",
       requires=["character_controller"],
       steps=[
           ("Add an interactable NPC",
            "screenshot of the NPC"),
           ("Implement proximity/interaction triggering a conversation",
            "logs showing the conversation starting"),
           ("Present dialogue lines and let the player advance/choose",
            "screenshot of the dialogue UI"),
       ],
       checklist=["the player can start a conversation",
                  "lines advance without dead-ends",
                  "the conversation can be exited"],
       risks=["dialogue opens and traps the player",
              "choice leads nowhere"],
       tags=["dialogue", "talk", "conversation", "npc", "interact",
             "choice", "story"]),

    _r("vehicle", "Vehicle", GAMEPLAY,
       "the player can drive/ride something",
       requires=["character_controller"],
       steps=[
           ("Create the vehicle in the level", "screenshot of the vehicle"),
           ("Implement entering and exiting the vehicle",
            "state showing the player attached to it"),
           ("Implement vehicle steering and throttle",
            "state showing position changing under control"),
       ],
       checklist=["the player can enter and exit",
                  "the vehicle responds to steering",
                  "exiting leaves the player in a valid state"],
       risks=["player stuck inside the vehicle",
              "vehicle physics explode on spawn"],
       tags=["vehicle", "car", "drive", "ride", "boat", "plane"]),

    _r("save_load", "Save / load", SYSTEMS,
       "progress survives leaving the game",
       steps=[
           ("Define the save payload (progress, position, inventory)",
            "state showing the serialized payload"),
           ("Implement saving on a trigger (checkpoint or manual)",
            "logs showing the save"),
           ("Implement loading and restoring the saved state",
            "state after load matching the save"),
       ],
       checklist=["saving reports success",
                  "loading restores the saved state",
                  "loading an absent/corrupt save does not crash"],
       risks=["save writes but load is a no-op",
              "loading resets unrelated state"],
       tags=["save", "load", "checkpoint", "persist", "progress"]),

    # ----- ui -------------------------------------------------------------
    _r("hud", "HUD", UI,
       "the player can see their state",
       requires=["core_loop"],
       steps=[
           ("Display the core player stat (health/score/ammo)",
            "screenshot of the HUD element"),
           ("Update the HUD when the value changes",
            "screenshot after the value changed"),
       ],
       checklist=["the HUD is visible during play",
                  "the HUD reflects real state (not a static value)",
                  "the HUD does not cover the play area"],
       risks=["HUD shows a placeholder value forever",
              "HUD covers the whole screen"],
       tags=["hud", "ui", "score", "ammo", "display", "gui"]),

    _r("menu_pause", "Menu & pause", UI,
       "the player can start, pause and quit cleanly",
       steps=[
           ("Create a start menu with a working start action",
            "screenshot of the menu"),
           ("Implement pause/resume during play",
            "logs/state showing the paused state"),
       ],
       checklist=["the game can be started from the menu",
                  "pause actually stops simulation",
                  "resume returns to a playable state"],
       risks=["pause leaves the game frozen after resume",
              "menu button does nothing"],
       tags=["menu", "pause", "start", "quit", "settings", "options"]),

    _r("audio_feedback", "Audio feedback", SYSTEMS,
       "actions are audible",
       steps=[
           ("Add the main action's sound effect",
            "logs showing the audio playing"),
           ("Add background/ambient audio",
            "logs showing the ambient track running"),
       ],
       checklist=["the primary action is audible",
                  "audio does not spam on repeated triggers"],
       risks=["sound plays on every frame",
              "no audio assets reference the real events"],
       tags=["audio", "sound", "music", "sfx", "ambient"]),
]

RECIPES_BY_ID: Dict[str, Recipe] = {r.id: r for r in RECIPES}


# ---------------------------------------------------------------------------
# Selection (deterministic, lexical, no LLM needed)
# ---------------------------------------------------------------------------

# Vocabulary that maps a plain game request to recipe tags. Deliberately
# genre-shaped but implementation-agnostic: it decides WHICH recipes are
# relevant, never HOW they are built.
_REQUEST_HINTS: Dict[str, tuple] = {
    "character_controller": ("platformer", "rpg", "fps", "shooter", "action",
                             "adventure", "walk", "jump", "movement",
                             "character", "controller", "player", "runner",
                             "third person", "first person", "open world"),
    "level_blockout": ("level", "map", "world", "dungeon", "course", "stage",
                       "arena", "terrain", "island", "city", "open world",
                       "sandbox", "platformer"),
    "lighting_atmosphere": ("horror", "atmosphere", "mood", "night", "dark",
                            "open world", "realistic", "stylized"),
    "core_loop": ("game", "play", "fun", "loop", "score", "goal",
                  "objective", "arcade", "roguelike", "survival"),
    "health_damage": ("combat", "fight", "battle", "health", "damage",
                      "survival", "roguelike", "action", "rpg", "shooter",
                      "boss", "enemy"),
    "enemy_ai": ("enemy", "ai", "monster", "combat", "fight", "battle",
                 "boss", "shooter", "wave", "survival", "zombie"),
    "weapon_system": ("weapon", "gun", "shoot", "shooter", "combat",
                      "sword", "melee", "fight", "battle", "fps"),
    "inventory": ("inventory", "item", "pickup", "loot", "craft",
                  "rpg", "adventure", "collect", "backpack"),
    "quest_system": ("quest", "mission", "objective", "rpg", "adventure",
                     "story", "campaign", "tasks"),
    "dialogue": ("dialogue", "talk", "npc", "story", "rpg", "adventure",
                 "choice", "character"),
    "vehicle": ("car", "vehicle", "drive", "racing", "boat", "plane",
                "kart"),
    "save_load": ("save", "persist", "checkpoint", "progress", "rpg",
                  "campaign", "long"),
    "hud": ("score", "hud", "ui", "health bar", "ammo", "arcade", "fps",
            "shooter", "rpg", "racing"),
    "menu_pause": ("menu", "pause", "start screen", "settings", "options"),
    "audio_feedback": ("audio", "sound", "music", "atmosphere", "juice",
                       "polish", "feedback"),
}


def _tokens(text: str) -> List[str]:
    return [t for t in "".join(
        c.lower() if (c.isalnum() or c in " -") else " "
        for c in (text or "")).split() if t]


def score_recipe(recipe: Recipe, goal: str) -> int:
    """Lexical relevance of a recipe to a plain-language game request."""
    g = (goal or "").lower()
    score = 0
    for hint in _REQUEST_HINTS.get(recipe.id, ()):
        if hint in g:
            score += 3 if " " in hint else 2
    for tag in recipe.tags:
        if tag in g:
            score += 2
        elif tag in (recipe.system or ""):
            score += 0
    for tok in _tokens(recipe.provides):
        if tok in g and len(tok) > 3:
            score += 1
    return score


def select_recipes(goal: str, limit: int = 6,
                   min_score: int = 2) -> List[Recipe]:
    """Pick the recipes a request needs. Deterministic + explainable.

    Every game gets the two structural roots (a character and a space to
    play in) even when the request is worded oddly — a game without a
    playable space is not a game. Everything else must earn its place by
    matching the request.
    """
    roots = [r for r in (RECIPES_BY_ID.get("character_controller"),
                         RECIPES_BY_ID.get("level_blockout")) if r]
    scored = sorted(
        ((score_recipe(r, goal), r) for r in RECIPES
         if r.id not in {x.id for x in roots}),
        key=lambda pair: (-pair[0], pair[1].id))
    picked = list(roots)
    for score, r in scored:
        if len(picked) >= limit:
            break
        if score >= min_score:
            picked.append(r)
    # Dependency closure: a picked recipe's requirements must be present.
    by_id = {r.id: r for r in picked}
    changed = True
    while changed:
        changed = False
        for r in list(by_id.values()):
            for req in r.requires:
                if req not in by_id and req in RECIPES_BY_ID:
                    if len(by_id) >= limit + 3:   # hard bound
                        continue
                    by_id[req] = RECIPES_BY_ID[req]
                    changed = True
    # Stable, dependency-first order.
    ordered: List[Recipe] = []
    seen: set = set()

    def _add(r: Recipe) -> None:
        if r.id in seen:
            return
        seen.add(r.id)
        for req in r.requires:
            if req in by_id:
                _add(by_id[req])
        ordered.append(r)

    for r in RECIPES:
        if r.id in by_id:
            _add(by_id[r.id])
    return ordered


def recipe_for(system_or_recipe_id: str) -> Optional[Recipe]:
    return RECIPES_BY_ID.get((system_or_recipe_id or "").strip().lower())


def library_summary(limit: int = 40) -> List[Dict[str, Any]]:
    return [r.to_dict() for r in RECIPES[:limit]]


def checklist_for(system_ids: List[str], max_items: int = 10) -> List[str]:
    """Merged, de-duplicated completion criteria for a set of systems."""
    out: List[str] = []
    for sid in system_ids or []:
        r = recipe_for(sid)
        if not r:
            continue
        for item in r.checklist:
            if item not in out:
                out.append(item)
            if len(out) >= max_items:
                return out
    return out
