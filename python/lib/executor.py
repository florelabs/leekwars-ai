# Exécution d'un plan : le seul module (avec world.snapshot) qui agit sur l'API.
#
# Piège du runtime (objects.py) : `me.moveToward(x)` avec un int traite x comme un id d'ENTITÉ ; pour une
# case il faut passer un objet `Cell` (→ moveTowardCell). On vérifie l'arrivée après chaque déplacement et
# on explique chaque échec d'attaque en mode debug (distance, LOS) : un tour raté doit se lire dans le journal.

from planner import Action, Plan, Planner, focus
from team import TeamState
from tuning import Profile
from world import World


def execute(world: World, plan: Plan, debug: bool = False) -> None:
    me = Fight.me
    for a in plan.actions:
        if debug:
            Debug.log(str(a))
        if a.kind == "move" and a.cell is not None and me.cell.id != a.cell:
            target = Cell.get(a.cell)
            if target is None:
                continue
            me.moveToward(target)
            if me.cell.id != a.cell:
                Debug.log(f"move raté : voulu {a.cell}, arrivé {me.cell.id}, PM restants {me.mp}", Color.RED)
        elif a.kind == "teleport" and a.skill is not None and a.cell is not None:
            r = me.useChipOnCell(a.skill.item, a.cell)
            if r <= 0:
                _explain(me, a, r, a.cell)
        elif a.kind == "weapon":
            _use_weapon(me, a)
        elif a.kind == "chip" and a.skill is not None and a.target is not None:
            r = me.useChip(a.skill.item, a.target)
            if r <= 0:
                _explain(me, a, r)


def announce(world: World, plan: Plan, planner: Planner, profile: Profile) -> None:
    """Publie à l'équipe (40 ops) ma cible principale et si j'ai engagé : un kill, ou un plan qui vaut au
    moins `engage_share` de mon alpha."""
    if not world.allies:
        return
    engaged = bool(plan.kills) or plan.value >= profile.engage_share * max(1.0, planner.d.my_alpha())
    Network.sendAll(Message.Type.CUSTOM, TeamState.encode(world.turn, focus["target"], engaged))


def _use_weapon(me: Me, a: Action) -> None:
    if a.skill is None or a.target is None:
        return
    w = a.skill.item
    if me.weapon is not w:
        me.setWeapon(w)
    for _ in range(a.n):
        r = me.useWeapon(a.target)
        if r <= 0:
            _explain(me, a, r)
            break


def _explain(me: Me, a: Action, code: int, cell: int | None = None) -> None:
    """Journalise pourquoi une action a échoué (≈ 100 ops : seulement sur échec)."""
    key = a.skill.key if a.skill else "?"
    if cell is None and a.target is not None:
        ent = Entity.get(a.target)
        cell = ent.cell.id if ent is not None else None
    if cell is None:
        Debug.log(f"{key} → {code} (cible {a.target} introuvable)", Color.RED)
        return
    Debug.log(f"{key} → {code} depuis {me.cell.id} : distance {Field.distance(me.cell, cell)}, "
              f"LOS {Field.lineOfSight(me.cell, cell)}, PT {me.tp}", Color.RED)
