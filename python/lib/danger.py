# Map de danger : dégâts attendus au prochain tour ennemi, pour N'IMPORTE quelle case, en O(1) par case.
#
# Pour chaque ennemi e (et chaque variante d'état : de base, PM entravés, PT entravés) :
#   1. reach_e  = BFS à pied avec sa mobilité (PM max + bottes ; entités = obstacles)     ~3 k ops
#   2. dist_e   = champ de distance (sans obstacles) depuis reach_e sur toute la grille   ~13 k ops
#   3. dmg_e[d] = meilleure dépense de ses PT max en ne gardant que ses skills de portée ≥ d
#   danger(c) = Σ_e dmg_e[dist_e[c]]
#
# Pessimiste par construction : LOS ennemie ignorée, portée min ignorée (il peut reculer), tout supposé
# disponible (cooldowns ennemis non lus). Les types de lancer LINE/DIAGONAL sont approximés en cercle.
# La téléportation ennemie n'entre PAS dans la map (sinon tout est rouge) : elle compte dans `engage()`.
#
# Symétrique : `pressure(c)` = ce que JE pourrais infliger au prochain tour depuis c (PM/PT max), et
# `engage(c)` ∈ [0, 1] = probabilité grossière d'un échange au prochain tour si je finis en c. Ces deux
# valeurs servent à ne pas fuir indéfiniment et à se buffer AVANT le contact.

from dataclasses import replace

import formulas
from geometry import INF, LAUNCH_CIRCLE, Grid, bfs_walk, dist_field, launch_ok
from skills import BUFF_MP, DAMAGE, POISON, TELEPORT, Skill
from world import Ent, World

BASE = "base"
SHACKLED_MP = "mp"
SHACKLED_TP = "tp"


def best_spend(options: list[tuple[int, float, int]], tp: int) -> float:
    """Sac à dos borné : options (coût, valeur unitaire, usages max ; 0 = illimité) → meilleure valeur pour `tp`."""
    best = [0.0] * (tp + 1)
    for cost, value, max_uses in options:
        if cost <= 0 or cost > tp:
            continue
        cap = tp // cost if max_uses <= 0 else min(max_uses, tp // cost)
        # Répétition bornée : on déroule les usages (cap petit : ≤ 4 pour les armes, ≤ 6 pour les puces).
        for _ in range(cap):
            for t in range(tp, cost - 1, -1):
                v = best[t - cost] + value
                if v > best[t]:
                    best[t] = v
    return best[tp]


def unit_damage(sk: Skill, caster: Ent, target: Ent, poison_discount: float = 1.0) -> float:
    """Dégâts moyens d'UN usage de `sk` par `caster` sur `target` (poison : total sur ses tours, décoté)."""
    if sk.kind == DAMAGE:
        return formulas.damage(sk.avg, caster.strength, caster.power, target.rel_shield, target.abs_shield) * \
            formulas.expected_mult(caster.agility)
    if sk.kind == POISON:
        per_turn = formulas.poison(sk.avg, caster.magic, caster.power)
        total = 0.0
        w = 1.0
        for _ in range(max(1, sk.turns)):
            total += per_turn * w
            w *= poison_discount
        return total
    return 0.0


def damage_by_range(attacker: Ent, target: Ent, tp: int, poison_discount: float) -> list[float]:
    """dmg[d] = meilleure dépense de `tp` par `attacker` sur `target` avec ses skills de portée max ≥ d.
    Longueur = portée max + 2 (dernier élément = 0 : hors d'atteinte)."""
    options = []
    max_range = 0
    for sk in attacker.skills:
        if sk.kind not in (DAMAGE, POISON) or not sk.available:
            continue
        v = unit_damage(sk, attacker, target, poison_discount)
        if v <= 0:
            continue
        options.append((sk.cost, v, sk.max_uses, sk.max_range))
        if sk.max_range > max_range:
            max_range = sk.max_range
    dmg_at = [0.0] * (max_range + 2)
    # dmg_at[d] ne change qu'aux portées max des skills : un sac à dos par seuil distinct, pas par distance.
    thresholds = sorted({r for _c, _v, _u, r in options}, reverse=True)
    thresholds.append(-1)
    for i in range(len(thresholds) - 1):
        r = thresholds[i]
        v = best_spend([(c, v, u) for c, v, u, rr in options if rr >= r], tp)
        for d in range(thresholds[i + 1] + 1, r + 1):
            dmg_at[d] = v
    return dmg_at


def mobility(e: Ent) -> int:
    """PM max + bottes disponibles (pessimiste : on suppose qu'il les utilise)."""
    m = e.max_mp
    for sk in e.skills:
        if sk.kind == BUFF_MP and sk.available:
            m += round(sk.avg)
    return m


def has_teleport(e: Ent) -> bool:
    return any(sk.kind == TELEPORT and sk.available for sk in e.skills)


class Danger:
    def __init__(self, world: World, poison_discount: float = 0.7) -> None:
        self.world = world
        self.grid: Grid = world.grid
        self.poison_discount = poison_discount
        self._fields: dict[tuple[int, str], list[float]] = {}
        self._alpha: dict[tuple[int, str], float] = {}
        self._reach: dict[tuple[int, str], dict[int, int]] = {}
        self._combined: dict[tuple, list[float]] = {}
        self._dist: dict[int, list[int]] = {}  # champ de distance à la zone atteignable de chaque ennemi (base)
        self._depth: list[int] | None = None
        self._my_dmg: dict[int, list[float]] = {}  # mes dégâts par distance, par ennemi
        self._pressure: list[float] | None = None
        self.enemies: dict[int, Ent] = {e.id: e for e in world.enemies}

    # ---- danger ennemi → moi ------------------------------------------------------------------------

    def variant_of(self, e: Ent, variant: str, amount: float = 0.0) -> Ent:
        if variant == SHACKLED_MP:
            return replace(e, max_mp=max(0, e.max_mp - round(amount)))
        if variant == SHACKLED_TP:
            return replace(e, max_tp=max(0, e.max_tp - round(amount)))
        return e

    def field(self, e: Ent, variant: str = BASE, amount: float = 0.0) -> list[float]:
        key = (e.id, variant)
        f = self._fields.get(key)
        if f is None:
            f = self._compute(self.variant_of(e, variant, amount), key)
            self._fields[key] = f
        return f

    def _compute(self, e: Ent, key: tuple[int, str]) -> list[float]:
        world = self.world
        reach = bfs_walk(self.grid, {e.cell: 0}, mobility(e), world.blocked_for(e))
        self._reach[key] = reach
        dist = dist_field(self.grid, list(reach))
        self._dist.setdefault(e.id, dist)
        dmg_at = damage_by_range(e, world.me, e.max_tp, self.poison_discount)
        self._alpha[key] = dmg_at[0]
        out = [0.0] * self.grid.n
        last = len(dmg_at) - 1
        for c in range(self.grid.n):
            d = dist[c]
            out[c] = dmg_at[d] if d < last else 0.0
        return out

    def alpha(self, e: Ent, variant: str = BASE, amount: float = 0.0) -> float:
        """Dégâts max de `e` sur moi en un tour s'il est à portée (état donné)."""
        self.field(e, variant, amount)
        return self._alpha[(e.id, variant)]

    def combined(self, alive: dict[int, tuple[str, float]] | None = None) -> list[float]:
        """Champ de danger total pour un état ennemi donné, caché : une passe sur la grille par état distinct,
        puis O(1) par case. `alive` = {id ennemi: (variante, montant)} ; None = tous, état de base."""
        if alive is None:
            alive = {e.id: (BASE, 0.0) for e in self.enemies.values()}
        key = tuple(sorted((eid, v) for eid, (v, _a) in alive.items()))
        total = self._combined.get(key)
        if total is None:
            fields = []
            for eid, (variant, amount) in alive.items():
                e = self.enemies.get(eid)
                if e is not None:
                    fields.append(self.field(e, variant, amount))
            if not fields:
                total = [0.0] * self.grid.n
            elif len(fields) == 1:
                total = fields[0]
            else:
                total = list(fields[0])
                for f in fields[1:]:
                    for c in range(self.grid.n):
                        total[c] += f[c]
            self._combined[key] = total
        return total

    def at(self, cell: int, alive: dict[int, tuple[str, float]] | None = None) -> float:
        return self.combined(alive)[cell]

    def depth(self) -> list[int]:
        """Distance de chaque case à la zone atteignable ennemie la plus proche (état de base) : sert de
        tie-break au repli (plus c'est grand, plus on est loin de tout)."""
        depth = self._depth
        if depth is None:
            fields: list[list[int]] = []
            for e in self.enemies.values():
                self.field(e)  # garantit self._dist[e.id]
                fields.append(self._dist[e.id])
            if not fields:
                depth = [INF] * self.grid.n
            elif len(fields) == 1:
                depth = fields[0]
            else:
                depth = list(fields[0])
                for f in fields[1:]:
                    for c in range(self.grid.n):
                        if f[c] < depth[c]:
                            depth[c] = f[c]
            self._depth = depth
        return depth

    # ---- alliés ------------------------------------------------------------------------------------

    def danger_vs(self, target: Ent, cell: int, alive: dict[int, tuple[str, float]] | None = None) -> float:
        """Danger d'une case pour un ALLIÉ (ses boucliers, pas les miens) : réutilise les champs de distance
        des ennemis, seul le sac à dos de dégâts est recalculé (caché par ennemi × cible)."""
        cache = self.__dict__.setdefault("_dmg_vs", {})
        ids = alive if alive is not None else {e.id: (BASE, 0.0) for e in self.enemies.values()}
        total = 0.0
        for eid, (variant, amount) in ids.items():
            e = self.enemies.get(eid)
            if e is None:
                continue
            self.field(e, variant, amount)  # garantit _dist[e.id]
            ev = self.variant_of(e, variant, amount)
            key = (eid, variant, target.id)
            dmg = cache.get(key)
            if dmg is None:
                dmg = damage_by_range(ev, target, ev.max_tp, self.poison_discount)
                cache[key] = dmg
            d = self._dist[eid][cell]
            if d < len(dmg) - 1:
                total += dmg[d]
        return total

    def alpha_of(self, ent: Ent) -> float:
        """Dégâts max en un tour (PT max) de `ent` sur l'ennemi le plus rentable (caché sur ses stats)."""
        cache = self.__dict__.setdefault("_alpha_of", {})
        key = (ent.id, ent.strength, ent.magic, ent.agility, ent.power, ent.max_tp)
        a = cache.get(key)
        if a is None:
            a = 0.0
            for e in self.enemies.values():
                v = damage_by_range(ent, e, ent.max_tp, self.poison_discount)[0]
                if v > a:
                    a = v
            cache[key] = a
        return a

    # ---- pression moi → ennemi, engagement -----------------------------------------------------------

    def my_alpha_vs(self, e: Ent) -> float:
        """Mes dégâts max en un tour (PT max) sur `e` précisément."""
        dmg = self._my_dmg.get(e.id)
        if dmg is None:
            me = self.world.me
            dmg = damage_by_range(me, e, me.max_tp, self.poison_discount)
            self._my_dmg[e.id] = dmg
        return dmg[0]

    def my_alpha(self, me: Ent | None = None) -> float:
        """Mes dégâts max en un tour (PT max) sur l'ennemi le plus rentable, pour un `me` donné."""
        me = me or self.world.me
        best = 0.0
        for e in self.enemies.values():
            v = damage_by_range(me, e, me.max_tp, self.poison_discount)[0]
            if v > best:
                best = v
        return best

    def pressure_field(self) -> list[float]:
        """Pour chaque case : dégâts que je pourrais infliger au prochain tour si j'y termine (PM/PT max,
        obstacles ignorés, meilleur ennemi). Une passe sur la grille par tour."""
        p = self._pressure
        if p is None:
            me = self.world.me
            n = self.grid.n
            p = [0.0] * n
            xy = self.grid.xy
            for e in self.enemies.values():
                dmg = damage_by_range(me, e, me.max_tp, self.poison_discount)
                self._my_dmg[e.id] = dmg
                last = len(dmg) - 1
                ex, ey = xy[e.cell]
                mp = me.max_mp
                for c in range(n):
                    x, y = xy[c]
                    d = abs(x - ex) + abs(y - ey) - mp
                    if d < 0:
                        d = 0
                    if d < last and dmg[d] > p[c]:
                        p[c] = dmg[d]
            self._pressure = p
        return p

    def pressure(self, cell: int) -> float:
        return self.pressure_field()[cell]

    def engage(self, cell: int, alive: dict[int, tuple[str, float]] | None = None) -> float:
        """∈ [0, 1] : à quel point un échange est probable au prochain tour si je finis sur `cell`.
        1 si un ennemi peut m'atteindre (map de danger, ou téléportation + mobilité + portée), sinon la part
        de mon alpha que je pourrais placer depuis là."""
        if self.at(cell, alive) > 0:
            return 1.0
        ids = alive.keys() if alive is not None else self.enemies.keys()
        for eid in ids:
            e = self.enemies.get(eid)
            if e is None or not has_teleport(e):
                continue
            tp_sk = next(sk for sk in e.skills if sk.kind == TELEPORT)
            reach = mobility(e) + tp_sk.max_range + max((sk.max_range for sk in e.skills
                                                         if sk.kind in (DAMAGE, POISON)), default=0)
            if self.grid.dist(cell, e.cell) <= reach:
                return 1.0
        alpha = self.my_alpha()
        return min(1.0, self.pressure(cell) / alpha) if alpha > 0 else 0.0

    def refined(self, cell: int, alive: dict[int, tuple[str, float]] | None = None, max_los: int = 40) -> float:
        """Danger de `cell` avec la VRAIE ligne de vue ennemie : un skill à LOS ne compte que s'il existe une
        case atteignable par l'ennemi, à portée, qui voit `cell`. Coûte jusqu'à `max_los` × 31 ops par
        ennemi ; au-delà du plafond on retombe sur l'estimation pessimiste. Toujours ≤ `at(cell)`."""
        if alive is None:
            alive = {e.id: (BASE, 0.0) for e in self.enemies.values()}
        grid = self.grid
        los = self.world.los
        me = self.world.me
        total = 0.0
        for eid, (variant, amount) in alive.items():
            e = self.enemies.get(eid)
            if e is None:
                continue
            field = self.field(e, variant, amount)
            if field[cell] == 0.0:
                continue
            ev = self.variant_of(e, variant, amount)
            reach = self._reach[(e.id, variant)]
            xy = grid.xy
            cx, cy = xy[cell]
            # Cases atteignables triées par distance à `cell` (calcul inline : c'est la boucle chaude).
            by_dist = sorted((abs(xy[r][0] - cx) + abs(xy[r][1] - cy), r) for r in reach)
            options = []
            budget = max_los
            for sk in ev.skills:
                if sk.kind not in (DAMAGE, POISON) or not sk.available:
                    continue
                v = unit_damage(sk, ev, me, self.poison_discount)
                if v <= 0:
                    continue
                hit = False
                circle = sk.launch == LAUNCH_CIRCLE
                for dist, r in by_dist:
                    if dist > sk.max_range:
                        break
                    if dist < sk.min_range:
                        continue
                    if not circle and not launch_ok(sk.launch, cx - xy[r][0], cy - xy[r][1]):
                        continue
                    if not sk.los or budget <= 0:  # sans LOS, ou plafond atteint : pessimiste
                        hit = True
                        break
                    budget -= 1
                    if los(r, cell):
                        hit = True
                        break
                if hit:
                    options.append((sk.cost, v, sk.max_uses))
            if options:
                total += best_spend(options, ev.max_tp)
        return total

    def exposure(self, cell: int, alive: dict[int, tuple[str, float]] | None = None) -> float:
        """Dégâts contre lesquels se protéger si je finis sur `cell` : danger réel, ou alpha ennemi pondéré par
        la probabilité d'engagement (téléportation adverse, contact imminent)."""
        danger = self.at(cell, alive)
        ids = alive if alive is not None else {e.id: (BASE, 0.0) for e in self.enemies.values()}
        total_alpha = 0.0
        for eid, (variant, amount) in ids.items():
            e = self.enemies.get(eid)
            if e is not None:
                total_alpha += self.alpha(e, variant, amount)
        return max(danger, self.engage(cell, alive) * total_alpha)
