# Géométrie en Python pur : grille, BFS, champs de distance, patterns de portée.
#
# Tout ici est indépendant de l'API de combat : la grille est construite une fois par `world.snapshot()`
# (coordonnées lues sur les 613 cases) ou par `Grid.from_ascii()` dans les tests. Les cases sont des ints
# (ids), jamais des objets `Cell` : un BFS sur 613 cases coûte ~15 k ops ici, contre 38 k pour UN seul
# `me.weaponCell()` côté moteur.

from collections import deque

INF = 1 << 30

# Types de lancer (Item.LaunchType.*) et zones (Item.Area.*), valeurs numériques des constantes du site.
LAUNCH_LINE = 1
LAUNCH_DIAGONAL = 2
LAUNCH_STAR = 3
LAUNCH_STAR_INVERTED = 4
LAUNCH_DIAGONAL_INVERTED = 5
LAUNCH_LINE_INVERTED = 6
LAUNCH_CIRCLE = 7

AREA_POINT = 1


def launch_ok(launch: int, dx: int, dy: int) -> bool:
    """La case (dx, dy) relative au lanceur respecte-t-elle le type de lancer ?"""
    if launch == LAUNCH_CIRCLE:
        return True
    line = dx == 0 or dy == 0
    diag = dx == dy or dx == -dy
    if launch == LAUNCH_LINE:
        return line
    if launch == LAUNCH_DIAGONAL:
        return diag
    if launch == LAUNCH_STAR:
        return line or diag
    if launch == LAUNCH_LINE_INVERTED:
        return not line
    if launch == LAUNCH_DIAGONAL_INVERTED:
        return not diag
    if launch == LAUNCH_STAR_INVERTED:
        return not (line or diag)
    return True


class Grid:
    """Grille statique : coordonnées dans le repère des voisins (4-connexité), obstacles fixes.

    `dist(a, b)` = |dx| + |dy| dans ce repère, ce qui correspond à `getCellDistance` sur le site
    (vérifié au tour 1 par `world.snapshot()` sur quelques paires).
    """

    def __init__(self, xy: list[tuple[int, int]], obstacle: list[bool]) -> None:
        self.n = len(xy)
        self.xy = xy
        self.obstacle = obstacle
        self.by_xy: dict[tuple[int, int], int] = {p: i for i, p in enumerate(xy)}
        by = self.by_xy
        self.neighbors: list[list[int]] = []
        for x, y in xy:
            nb = []
            for q in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                j = by.get(q)
                if j is not None:
                    nb.append(j)
            self.neighbors.append(nb)

    @classmethod
    def from_ascii(cls, rows: list[str]) -> "Grid":
        """Grille carrée de test : '#' obstacle, tout le reste libre. id = y * largeur + x."""
        xy = []
        obstacle = []
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                xy.append((x, y))
                obstacle.append(ch == "#")
        return cls(xy, obstacle)

    def cover_field(self) -> list[int]:
        """Pour chaque case, nombre d'obstacles à distance ≤ 2 (statique, calculé une fois) : proxy de
        « cachette »."""
        field = self.__dict__.get("_cover")
        if field is None:
            by = self.by_xy
            obstacle = self.obstacle
            offsets = [(dx, dy) for dx in range(-2, 3) for dy in range(-2 + abs(dx), 3 - abs(dx))]
            field = [0] * self.n
            for c, (cx, cy) in enumerate(self.xy):
                v = 0
                for dx, dy in offsets:
                    j = by.get((cx + dx, cy + dy))
                    if j is not None and obstacle[j]:
                        v += 1
                field[c] = v
            self.__dict__["_cover"] = field
        return field

    def cover(self, c: int) -> int:
        return self.cover_field()[c]

    def dist(self, a: int, b: int) -> int:
        ax, ay = self.xy[a]
        bx, by = self.xy[b]
        return abs(ax - bx) + abs(ay - by)

    def ring(self, center: int, min_r: int, max_r: int, launch: int = LAUNCH_CIRCLE) -> list[int]:
        """Cases à distance [min_r, max_r] de `center` compatibles avec le type de lancer.

        Le pattern est symétrique : les cases d'où on peut lancer SUR `center` sont les mêmes que
        celles que l'on peut viser DEPUIS `center`.
        """
        cx, cy = self.xy[center]
        by = self.by_xy
        out = []
        circle = launch == LAUNCH_CIRCLE
        for dx in range(-max_r, max_r + 1):
            adx = abs(dx)
            rem = max_r - adx
            for dy in range(-rem, rem + 1):
                if adx + abs(dy) < min_r or not (circle or launch_ok(launch, dx, dy)):
                    continue
                c = by.get((cx + dx, cy + dy))
                if c is not None:
                    out.append(c)
        return out

    def in_range(self, frm: int, to: int, min_r: int, max_r: int, launch: int = LAUNCH_CIRCLE) -> bool:
        fx, fy = self.xy[frm]
        tx, ty = self.xy[to]
        dx = tx - fx
        dy = ty - fy
        d = abs(dx) + abs(dy)
        return min_r <= d <= max_r and launch_ok(launch, dx, dy)

    def los_ascii(self, a: int, b: int, blocked: list[bool]) -> bool:
        """LOS de test (segment échantillonné) : bloquée par toute case `blocked` strictement entre a et b.
        Le site utilise son propre Bresenham ; au runtime `World.los` délègue à `Field.lineOfSight`."""
        ax, ay = self.xy[a]
        bx, by = self.xy[b]
        steps = max(abs(bx - ax), abs(by - ay)) * 2
        if steps == 0:
            return True
        for i in range(1, steps):
            t = i / steps
            c = self.by_xy.get((round(ax + (bx - ax) * t), round(ay + (by - ay) * t)))
            if c is not None and c != a and c != b and blocked[c]:
                return False
        return True


def bfs_walk(grid: Grid, sources: dict[int, int], max_cost: int, blocked: list[bool]) -> dict[int, int]:
    """Cases atteignables à pied : {case: coût en PM}. `sources` = {case: coût initial}.
    Les cases bloquées (obstacles + entités) ne sont ni traversées ni atteintes."""
    cost = dict(sources)
    queue = deque(sources)
    neighbors = grid.neighbors
    while queue:
        c = queue.popleft()
        nc = cost[c] + 1
        if nc > max_cost:
            continue
        for n in neighbors[c]:
            if n not in cost and not blocked[n]:
                cost[n] = nc
                queue.append(n)
    return cost


def dist_field(grid: Grid, sources: list[int]) -> list[int]:
    """Distance de chaque case à la source la plus proche, SANS tenir compte des obstacles
    (une portée d'arme ignore les obstacles, seule la LOS compte). BFS multi-source sur toute la grille."""
    field = [INF] * grid.n
    queue = deque()
    for s in sources:
        if field[s] != 0:
            field[s] = 0
            queue.append(s)
    neighbors = grid.neighbors
    while queue:
        c = queue.popleft()
        nd = field[c] + 1
        for n in neighbors[c]:
            if field[n] > nd:
                field[n] = nd
                queue.append(n)
    return field
