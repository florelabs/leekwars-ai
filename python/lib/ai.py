# IA complète bâtie sur bot/ : profil qui s'adapte à la situation, overlay de la map de danger, filet de
# sécurité si le planificateur plante, mesure des opérations par phase.
#
# Sur le site : copier ce fichier + tous les bot/*.py dans le dossier de l'IA (imports plats par nom).
# En local : `npx pyright` le vérifie (bot/ est dans extraPaths), les tests de la lib sont dans tests/.

from danger import Danger
from executor import announce, execute
from planner import Plan, Planner
from tuning import Phases, Profile
from world import World, snapshot

DEBUG = True

# Profil de base : équilibré. Les poids sont en PV (cf docs/planner.md).
# Budget : `budget` est une part de System.maxOperations (cœurs × 1 M). Avec beaucoup de cœurs, le vrai
# plafond est le wall-clock de 5 s/tour : 0.25 sur 20 cœurs = 5 M d'ops, dépensés en profondeur de recherche.
# En équipe : `w_ally` = valeur d'un PV soigné/protégé/gagné sur un allié ; `ally_weights` par nom (le carry
# vaut plus, un bulbe moins). Sans entrée, chaque allié vaut w_ally.
BASE = Profile(w_safety=1.0, w_kill=150, w_low_life=0.5, w_tp_reserve=30,
               max_stops=3, beam=16, k_walk=8, refine_plans=6, budget=0.25,
               w_ally=1.0, ally_weights={})

scores: list[float] = []  # persiste entre les tours (les globales survivent, cf docs/runtime.md)


def beforeFight() -> None:
    """Optionnel : choisir un loadout avant le tour 1. Ne rien faire d'autre ici."""
    pass


def pick_profile(world: World) -> Profile:
    """Toute l'« intelligence » de haut niveau tient ici : on ne touche qu'aux poids, jamais à l'algo."""
    me = world.me
    my_ratio = me.life / me.max_life
    enemies = world.enemies
    if not enemies:
        return BASE
    weakest = min(e.life / e.max_life for e in enemies)

    if weakest < 0.25:
        # Un ennemi est à l'agonie : on prend des risques pour finir, et on se fiche des cibles pleines.
        return Profile(**{**BASE.__dict__, "w_safety": 0.4, "w_kill": 400, "w_low_life": 2.0})
    if my_ratio < 0.35:
        # Je suis bas : chaque PV encaissé compte double, et la téléportation redevient une option de fuite.
        return Profile(**{**BASE.__dict__, "w_safety": 2.0, "w_tp_reserve": 0})
    if my_ratio > weakest + 0.3:
        # J'ai l'avantage : pression.
        return Profile(**{**BASE.__dict__, "w_safety": 0.7})
    return BASE


def show_danger(world: World, danger: Danger) -> None:
    """Overlay : cases atteignables coloriées selon le danger (1 seul appel Debug.mark par couleur = 164 ops)."""
    field = danger.combined()
    reach = Planner(world, BASE, danger).reach_from(world.me.cell, world.me.mp)
    hi = [c for c in reach if field[c] >= world.me.life * 0.5]
    mid = [c for c in reach if 0 < field[c] < world.me.life * 0.5]
    safe = [c for c in reach if field[c] == 0]
    if hi:
        Debug.mark(hi, Color.RED)
    if mid:
        Debug.mark(mid, Color.rgb(255, 160, 0))
    if safe:
        Debug.mark(safe, Color.GREEN)


def fallback() -> None:
    """Si le planificateur lève une exception (bug, budget...), on joue quand même quelque chose de simple."""
    me = Fight.me
    enemy = Fight.getNearestEnemy()
    if enemy is None:
        return
    cell = me.weaponCell(enemy)  # 38 k ops : acceptable une fois, en secours seulement
    if cell is not None and cell is not me.cell:
        me.moveToward(cell)
    while me.weapon is not None and me.tp >= me.weapon.cost and me.useWeapon(enemy) > 0:
        pass
    me.moveAwayFrom(enemy)


def turn() -> None:
    world = snapshot()
    if not world.enemies:
        return
    if DEBUG and world.turn == 1:
        # Ce que la lib a compris du loadout : un skill absent ou en `other` ici = une puce ignorée par le planner.
        Debug.log("skills : " + ", ".join(f"{s.key}[{s.kind}{'' if s.available else ' cd'}]" for s in world.me.skills))
        Debug.log("ennemis : " + ", ".join(f"{e.name} {e.life}PV {e.tp}PT {e.mp}PM {len(e.skills)} skills"
                                            for e in world.enemies))
    profile = pick_profile(world)
    phases = Phases(world.ops)
    plan: Plan | None = None
    try:
        with phases.phase("danger"):
            danger = Danger(world, profile.poison_discount)
            for e in world.enemies:
                danger.field(e)
        with phases.phase("plan"):
            planner = Planner(world, profile, danger)
            plan = planner.plan()
        with phases.phase("exec"):
            execute(world, plan, DEBUG)
            announce(world, plan, planner, profile)
        if DEBUG:
            show_danger(world, danger)
            Debug.log(f"T{world.turn} safety={profile.w_safety} {plan.describe()}")
            Debug.log(f"{phases.summary()} évaluations={planner.evaluations}")
    except Exception as exc:  # en combat, mieux vaut un tour moyen qu'un tour perdu
        Debug.log(f"planner KO : {exc!r}", Color.RED)
        if plan is None:
            fallback()
    if plan is not None:
        scores.append(plan.score)
