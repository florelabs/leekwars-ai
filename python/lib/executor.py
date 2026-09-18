# Exécution d'un plan : le seul module (avec world.snapshot) qui agit sur l'API. Chaque action vérifie son
# retour ; un déplacement raté n'interrompt pas le tour (les attaques suivantes testent leur retour).

from planner import Action, Plan
from world import World


def execute(world: World, plan: Plan, debug: bool = False) -> None:
    me = Fight.me
    for a in plan.actions:
        if debug:
            Debug.log(str(a))
        if a.kind == "move" and a.cell is not None and me.cell.id != a.cell:
            me.moveToward(a.cell)
        elif a.kind == "teleport" and a.skill is not None and a.cell is not None:
            me.useChipOnCell(a.skill.item, a.cell)
        elif a.kind == "weapon":
            _use_weapon(me, a)
        elif a.kind == "chip" and a.skill is not None and a.target is not None:
            r = me.useChip(a.skill.item, a.target)
            if r <= 0 and debug:
                Debug.log(f"useChip {a.skill.key} → {r}", Color.RED)


def _use_weapon(me: Me, a: Action) -> None:
    if a.skill is None or a.target is None:
        return
    w = a.skill.item
    if me.weapon is not w:
        me.setWeapon(w)
    for _ in range(a.n):
        if me.useWeapon(a.target) <= 0:
            break
