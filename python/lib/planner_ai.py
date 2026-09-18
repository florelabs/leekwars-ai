# IA d'entrée : snapshot → danger → plan → exécution. Copier ce fichier et les modules de bot/ dans le
# dossier de l'IA sur le site ; régler `PROFILE` pour changer le comportement.

from danger import Danger
from executor import execute
from planner import Planner
from tuning import Phases, Profile
from world import snapshot

PROFILE = Profile(w_safety=1.0, max_stops=2, beam=8, budget=0.7, debug=True)


def turn() -> None:
    world = snapshot()
    phases = Phases(world.ops)
    with phases.phase("danger"):
        danger = Danger(world, PROFILE.poison_discount)
        for e in world.enemies:
            danger.field(e)
    with phases.phase("plan"):
        planner = Planner(world, PROFILE, danger)
        plan = planner.plan()
    with phases.phase("exec"):
        execute(world, plan, PROFILE.debug)
    if PROFILE.debug:
        Debug.log(f"T{world.turn} {plan.describe()}")
        Debug.log(f"{phases.summary()} évaluations={planner.evaluations}")
