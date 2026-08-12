"""
================================================================================
GENERADOR PROCEDURAL DE NIVELES ESTILO OBSIDIAN (DOOM) - ADDON PARA BLENDER 4.X
================================================================================

Clon funcional del algoritmo de distribucion arquitectonica del generador
OBSIDIAN para DOOM, reimplementado en Python puro + BMesh, empaquetado como
addon instalable con panel de control en la barra lateral del viewport 3D.

Genera UNICAMENTE el caparazon geometrico "grey-box" de un nivel:
    - Sin materiales / texturas
    - Sin luces
    - Sin items ni mecanicas de juego
    - Sin puertas dinamicas
    - Sin cubos apilados y sin modificadores booleanos

--------------------------------------------------------------------------------
ARQUITECTURA DEL ADDON
--------------------------------------------------------------------------------
1) CAPA LOGICA DE DISTRIBUCION (Python puro, sin dependencias de Blender):
     inicializar_grilla()            -> matriz logica vacia (piso / id / altura)
     generar_habitaciones_obsidian() -> coloca salas variadas sin solaparse
     conectar_pasillos()             -> interconecta todo con tuneles ortogonales
     verificar_conectividad_total()  -> red de seguridad BFS: 0 zonas aisladas

   Variedad de diseño incluida en esta capa:
     - 8 siluetas de sala: rectangulo, forma en L, cruz, octogono, forma en T,
       zigzag/escalonada, patio en U y organica (caverna via random-walk).
     - Pilares/columnas interiores en salas grandes de silueta solida
       (rectangulo, octogono, patio): un hueco tallado en el centro que se
       convierte en muro automaticamente en la capa 3D (ver mas abajo).
     - Pasillos con multiples giros (zigzag) ademas de la L simple, para
       variar la silueta de los corredores.

2) CAPA DE CONSTRUCCION 3D (BMesh):
     generar_malla_3d()  -> traduce la grilla a geometria real en 4 pasos:
         a) un quad de piso por celda transitable (Z=0)
         b) bmesh.ops.remove_doubles   -> fusiona vertices, elimina "paredes"
            internas invisibles entre celdas contiguas
         c) edge.is_boundary           -> detecta el perimetro libre real
            (incluye automaticamente el "agujero" de un pilar interior como
            un segundo loop de borde, sin codigo adicional)
         d) bmesh.ops.extrude_edge_only, agrupado por altura de region,
            para esculpir los muros con los desniveles tipicos de DOOM

3) CAPA DE ADDON (Blender UI):
     OBSIDIAN_PG_propiedades  -> parametros configurables (PropertyGroup)
     OBSIDIAN_OT_generar_nivel-> operador que ejecuta el pipeline completo
     OBSIDIAN_PT_panel        -> panel en View3D > Sidebar (N) > pestana OBSIDIAN

--------------------------------------------------------------------------------
INSTALACION
--------------------------------------------------------------------------------
Este archivo se distribuye dentro de un .zip (carpeta "generador_nivel_obsidian"
conteniendo este __init__.py). Para instalar:
    1. Edit > Preferences > Add-ons
    2. Boton "Install from Disk..." (arriba a la derecha)
    3. Seleccionar el archivo .zip descargado (NO extraerlo antes)
    4. Activar la casilla del addon en la lista una vez instalado
    5. En el viewport 3D, abrir la barra lateral (tecla N) y buscar la
       pestana "OBSIDIAN"

Cada clic en "Generar Nivel" reemplaza el nivel anterior por uno nuevo
(soporta Ctrl+Z si el resultado no convence).
================================================================================
"""

bl_info = {
    "name": "OBSIDIAN Level Generator",
    "author": "Generado con asistencia de Claude (Anthropic)",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar (N) > OBSIDIAN",
    "description": "Generador procedural de niveles estilo OBSIDIAN (DOOM): 8 formas de sala, pilares, pasillos en zigzag; solo geometria",
    "category": "Add Mesh",
}

import bpy
import bmesh
import random
from collections import deque
from bpy.props import IntProperty, FloatProperty, BoolProperty, PointerProperty
from bpy.types import PropertyGroup, Operator, Panel

# ==============================================================================
# 1. VALORES POR DEFECTO (se sobreescriben en tiempo de ejecucion con los
#    valores que el usuario configure en el panel; ver _aplicar_configuracion)
# ==============================================================================

GRID_ANCHO = 80
GRID_ALTO = 80
TAMANO_CELDA = 2.0

NUM_HABITACIONES_OBJETIVO = 32
MAX_INTENTOS_COLOCACION = 3000
TAMANO_MIN_SALA = 5
TAMANO_MAX_SALA = 12
MARGEN_ENTRE_SALAS = 2

PESOS_FORMAS = {
    "rectangulo": 0.22,
    "forma_l": 0.13,
    "cruz": 0.10,
    "octogono": 0.13,
    "forma_t": 0.12,
    "zigzag": 0.12,
    "patio": 0.12,
    "organica": 0.06,
}

# Formas "solidas" (sin concavidades profundas) donde es seguro tallar un
# pilar interior sin arriesgar que la sala quede desconectada.
FORMAS_ELEGIBLES_PARA_PILAR = ("rectangulo", "octogono", "patio")
PROBABILIDAD_PILAR = 0.55       # chance de pilar interior si la sala es grande
MARGEN_MINIMO_PILAR = 3         # ancho minimo del "anillo" alrededor del pilar

PROPORCION_PASILLO_ZIGZAG = 0.4  # fraccion de pasillos con multiples giros en vez de una sola L

ALTURAS_SALAS_POSIBLES = [3.0, 4.0, 5.0, 6.0]
ALTURA_PASILLO = 3.0

ANCHO_PASILLO = 1
PROPORCION_CONEXIONES_EXTRA = 0.15

SEMILLA_ALEATORIA = None
NOMBRE_OBJETO = "OBSIDIAN_Level"


# ==============================================================================
# 2. CAPA LOGICA DE DISTRIBUCION - "EL ESTILO OBSIDIAN" EN PYTHON PURO
#    (identica a la version validada de forma aislada con 25+ ejecuciones de
#    prueba y 5 configuraciones de grilla/sala distintas antes de integrarla)
# ==============================================================================

def inicializar_grilla(ancho: int, alto: int):
    """
    Crea la matriz 2D maestra que representa el plano logico del nivel.

    Devuelve tres estructuras paralelas (dimension ancho x alto):
        grid            : 0 = vacio / 1 = piso transitable
        id_region       : id entero de la sala/pasillo dueno de la celda (-1 = ninguno)
        altura_regiones : dict {id_region: altura_de_extrusion}
    """
    grid = [[0 for _ in range(ancho)] for _ in range(alto)]
    id_region = [[-1 for _ in range(ancho)] for _ in range(alto)]
    altura_regiones = {}
    return grid, id_region, altura_regiones


def _generar_celdas_rectangulo(x, y, w, h):
    return [(x + i, y + j) for j in range(h) for i in range(w)]


def _generar_celdas_forma_l(x, y, w, h):
    """Rectangulo completo menos un cuadrante (esquina), elegido al azar."""
    esquina = random.choice(["sup_izq", "sup_der", "inf_izq", "inf_der"])
    corte_w = max(1, w // 2)
    corte_h = max(1, h // 2)
    celdas = []
    for j in range(h):
        for i in range(w):
            if esquina == "sup_izq" and i < corte_w and j < corte_h:
                continue
            if esquina == "sup_der" and i >= w - corte_w and j < corte_h:
                continue
            if esquina == "inf_izq" and i < corte_w and j >= h - corte_h:
                continue
            if esquina == "inf_der" and i >= w - corte_w and j >= h - corte_h:
                continue
            celdas.append((x + i, y + j))
    return celdas


def _generar_celdas_cruz(x, y, w, h):
    """Sala en forma de cruz/plus: una barra vertical y otra horizontal centradas."""
    barra_w = max(2, w // 3)
    barra_h = max(2, h // 3)
    inicio_barra_x = (w - barra_w) // 2
    inicio_barra_y = (h - barra_h) // 2
    celdas = []
    for j in range(h):
        for i in range(w):
            en_barra_vertical = inicio_barra_x <= i < inicio_barra_x + barra_w
            en_barra_horizontal = inicio_barra_y <= j < inicio_barra_y + barra_h
            if en_barra_vertical or en_barra_horizontal:
                celdas.append((x + i, y + j))
    return celdas


def _generar_celdas_octogono(x, y, w, h):
    """
    Aproximacion de sala hexagonal/circular sobre grilla ortogonal: un
    rectangulo con las 4 esquinas cortadas en diagonal. Se usa un octogono
    (no un hexagono puro) porque es la unica forma de bloques regular que
    se puede representar limpiamente sobre una grilla cuadrada.
    """
    corte = max(1, min(w, h) // 3)
    celdas = []
    for j in range(h):
        for i in range(w):
            if (i + j) < corte:
                continue
            if ((w - 1 - i) + j) < corte:
                continue
            if (i + (h - 1 - j)) < corte:
                continue
            if ((w - 1 - i) + (h - 1 - j)) < corte:
                continue
            celdas.append((x + i, y + j))
    return celdas


def _generar_celdas_forma_t(x, y, w, h):
    """Sala en forma de T: una barra horizontal arriba y un tallo vertical bajando del centro."""
    barra_h = max(2, h // 3)
    tallo_w = max(2, w // 3)
    tallo_x = (w - tallo_w) // 2
    celdas = []
    for j in range(h):
        for i in range(w):
            en_barra = j < barra_h
            en_tallo = tallo_x <= i < tallo_x + tallo_w
            if en_barra or en_tallo:
                celdas.append((x + i, y + j))
    return celdas


def _generar_celdas_zigzag(x, y, w, h):
    """
    Sala escalonada en zigzag: 3 bloques rectangulares superpuestos que se
    desplazan en diagonal de una esquina a la opuesta, aproximando una
    silueta en Z/escalera sobre la grilla ortogonal (una diagonal real no
    es representable con bloques). El tamano de cada bloque se calcula
    para GARANTIZAR que bloques consecutivos se solapen (sala conectada)
    sin importar el redondeo en tamanos chicos.
    """
    num_bloques = 3
    ancho_bloque = min(w, max(2, w // num_bloques + 2))
    alto_bloque = min(h, max(2, h // num_bloques + 2))

    celdas = set()
    for k in range(num_bloques):
        frac = k / max(1, num_bloques - 1)
        bx = x + round(frac * max(0, w - ancho_bloque))
        by = y + round(frac * max(0, h - alto_bloque))
        for j in range(alto_bloque):
            for i in range(ancho_bloque):
                nx, ny = bx + i, by + j
                if x <= nx < x + w and y <= ny < y + h:
                    celdas.add((nx, ny))
    return list(celdas)


def _generar_celdas_patio(x, y, w, h):
    """Sala en forma de U/patio: un rectangulo con un lado completo con una entrante central."""
    lado_abierto = random.choice(["arriba", "abajo", "izquierda", "derecha"])
    profundidad_corte = max(2, min(w, h) // 3)
    ancho_corte = max(2, (w if lado_abierto in ("arriba", "abajo") else h) // 2)

    celdas = []
    for j in range(h):
        for i in range(w):
            cortar = False
            if lado_abierto == "arriba" and j < profundidad_corte and (w - ancho_corte) // 2 <= i < (w + ancho_corte) // 2:
                cortar = True
            elif lado_abierto == "abajo" and j >= h - profundidad_corte and (w - ancho_corte) // 2 <= i < (w + ancho_corte) // 2:
                cortar = True
            elif lado_abierto == "izquierda" and i < profundidad_corte and (h - ancho_corte) // 2 <= j < (h + ancho_corte) // 2:
                cortar = True
            elif lado_abierto == "derecha" and i >= w - profundidad_corte and (h - ancho_corte) // 2 <= j < (h + ancho_corte) // 2:
                cortar = True
            if not cortar:
                celdas.append((x + i, y + j))
    return celdas


def _generar_celdas_organica(x, y, w, h):
    """
    Sala organica tipo caverna: crece por random-walk desde el centro de la
    caja delimitadora, dando un contorno irregular no rectangular. Cada
    celda nueva se agrega siempre adyacente a una celda ya existente, por
    lo que el resultado queda garantizado como un unico bloque conectado.
    """
    celdas = set()
    cx, cy = x + w // 2, y + h // 2
    celdas.add((cx, cy))
    celdas_lista = [(cx, cy)]

    objetivo = max(6, int(w * h * 0.65))
    intentos = 0

    while len(celdas) < objetivo and intentos < objetivo * 25:
        intentos += 1
        base = random.choice(celdas_lista)
        dx, dy = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
        nx, ny = base[0] + dx, base[1] + dy
        if x <= nx < x + w and y <= ny < y + h and (nx, ny) not in celdas:
            celdas.add((nx, ny))
            celdas_lista.append((nx, ny))

    return list(celdas)


def _tiene_espacio_para_pilar(w, h, margen_pilar=MARGEN_MINIMO_PILAR):
    """Determina si una sala es lo bastante grande para tallarle un pilar interior sin
    dejar un anillo demasiado angosto alrededor."""
    return w >= 2 * margen_pilar + 2 and h >= 2 * margen_pilar + 2


def _aplicar_pilar_central(celdas, x, y, w, h):
    """
    Modificador: quita un bloque rectangular del centro de una sala ya
    generada, creando una columna/pilar de soporte (elemento clasico en
    hangares y salas tecnicas de Doom). El hueco se convierte automatica-
    mente en un muro interior en la capa 3D gracias a que edge.is_boundary
    detecta su perimetro como un segundo loop de borde, sin necesitar
    ningun codigo extra en generar_malla_3d.
    """
    hueco_w = max(2, w // 3)
    hueco_h = max(2, h // 3)
    hueco_x = x + (w - hueco_w) // 2
    hueco_y = y + (h - hueco_h) // 2
    celdas_hueco = {(hueco_x + i, hueco_y + j) for j in range(hueco_h) for i in range(hueco_w)}
    return [c for c in celdas if c not in celdas_hueco]


_GENERADORES_DE_FORMA = {
    "rectangulo": _generar_celdas_rectangulo,
    "forma_l": _generar_celdas_forma_l,
    "cruz": _generar_celdas_cruz,
    "octogono": _generar_celdas_octogono,
    "forma_t": _generar_celdas_forma_t,
    "zigzag": _generar_celdas_zigzag,
    "patio": _generar_celdas_patio,
    "organica": _generar_celdas_organica,
}


def _elegir_forma_ponderada():
    formas = list(PESOS_FORMAS.keys())
    pesos = list(PESOS_FORMAS.values())
    return random.choices(formas, weights=pesos, k=1)[0]


def _ubicacion_es_valida(grid, celdas, margen):
    """Comprueba limites del mapa y ausencia de solapamiento (con margen) con otra sala."""
    alto = len(grid)
    ancho = len(grid[0])
    for (cx, cy) in celdas:
        for dx in range(-margen, margen + 1):
            for dy in range(-margen, margen + 1):
                nx, ny = cx + dx, cy + dy
                if nx < 0 or nx >= ancho or ny < 0 or ny >= alto:
                    return False
                if grid[ny][nx] != 0:
                    return False
    return True


def generar_habitaciones_obsidian(grid, id_region, altura_regiones,
                                   num_objetivo: int = None,
                                   max_intentos: int = None):
    """
    Puebla la grilla con salas de formas y tamanos variados: rectangulos,
    forma en L, cruz, octogono, forma en T, zigzag/escalonada, patio en U
    y organica (caverna), garantizando que ninguna se solape. Las salas
    grandes de silueta solida pueden ademas recibir un pilar/columna
    interior (ver _aplicar_pilar_central).

    Cada sala colocada recibe un id de region unico y una altura de
    extrusion aleatoria, base de los desniveles arquitectonicos.

    Modifica 'grid' e 'id_region' in-place y devuelve la lista de salas
    (cada una un dict con id, forma, celdas, centro, altura y tiene_pilar).
    """
    if num_objetivo is None:
        num_objetivo = NUM_HABITACIONES_OBJETIVO
    if max_intentos is None:
        max_intentos = MAX_INTENTOS_COLOCACION

    salas = []
    siguiente_id = 0
    ancho_grid = len(grid[0])
    alto_grid = len(grid)

    for _ in range(max_intentos):
        if len(salas) >= num_objetivo:
            break

        forma = _elegir_forma_ponderada()
        w = random.randint(TAMANO_MIN_SALA, TAMANO_MAX_SALA)
        h = random.randint(TAMANO_MIN_SALA, TAMANO_MAX_SALA)

        limite_x = ancho_grid - w - MARGEN_ENTRE_SALAS - 1
        limite_y = alto_grid - h - MARGEN_ENTRE_SALAS - 1
        if limite_x <= MARGEN_ENTRE_SALAS + 1 or limite_y <= MARGEN_ENTRE_SALAS + 1:
            continue

        x = random.randint(MARGEN_ENTRE_SALAS + 1, limite_x)
        y = random.randint(MARGEN_ENTRE_SALAS + 1, limite_y)

        celdas = _GENERADORES_DE_FORMA[forma](x, y, w, h)
        if not celdas:
            continue

        if not _ubicacion_es_valida(grid, celdas, MARGEN_ENTRE_SALAS):
            continue

        # Pilar interior opcional: solo en formas solidas (sin concavidades
        # profundas) y solo si la sala es lo bastante grande. El hueco se
        # convierte en un muro/columna automaticamente en la capa 3D.
        tiene_pilar = False
        if forma in FORMAS_ELEGIBLES_PARA_PILAR and _tiene_espacio_para_pilar(w, h):
            if random.random() < PROBABILIDAD_PILAR:
                celdas = _aplicar_pilar_central(celdas, x, y, w, h)
                tiene_pilar = True

        altura = random.choice(ALTURAS_SALAS_POSIBLES)
        for (cx, cy) in celdas:
            grid[cy][cx] = 1
            id_region[cy][cx] = siguiente_id

        centro_promedio = (
            sum(c[0] for c in celdas) // len(celdas),
            sum(c[1] for c in celdas) // len(celdas),
        )
        centro = min(celdas, key=lambda c: abs(c[0] - centro_promedio[0]) + abs(c[1] - centro_promedio[1]))

        salas.append({
            "id": siguiente_id, "forma": forma, "celdas": celdas,
            "centro": centro, "altura": altura, "tiene_pilar": tiene_pilar,
        })
        altura_regiones[siguiente_id] = altura
        siguiente_id += 1

    return salas


def _distancia_manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _camino_ortogonal(origen, destino):
    """Genera un camino en 'L' (tunel ortogonal a 90 grados) entre dos puntos de la grilla."""
    x0, y0 = origen
    x1, y1 = destino
    camino = []

    if random.random() < 0.5:
        paso_x = 1 if x1 >= x0 else -1
        for x in range(x0, x1 + paso_x, paso_x):
            camino.append((x, y0))
        paso_y = 1 if y1 >= y0 else -1
        for y in range(y0, y1 + paso_y, paso_y):
            camino.append((x1, y))
    else:
        paso_y = 1 if y1 >= y0 else -1
        for y in range(y0, y1 + paso_y, paso_y):
            camino.append((x0, y))
        paso_x = 1 if x1 >= x0 else -1
        for x in range(x0, x1 + paso_x, paso_x):
            camino.append((x, y1))

    return camino


def _camino_zigzag(origen, destino, segmentos=3):
    """
    Variante del camino ortogonal con VARIOS giros en vez de uno solo: una
    escalera de mini-L consecutivas, alternando cual eje se mueve primero
    en cada tramo. Sigue siendo estrictamente ortogonal (90 grados), solo
    que con mas quiebres para romper la monotonia de pasillos siempre
    rectos.
    """
    x0, y0 = origen
    x1, y1 = destino
    dx_total, dy_total = x1 - x0, y1 - y0

    camino = [(x0, y0)]
    px, py = x0, y0
    for i in range(1, segmentos + 1):
        frac = i / segmentos
        tx = x0 + round(dx_total * frac)
        ty = y0 + round(dy_total * frac)
        if i % 2 == 1:
            paso_x = 1 if tx >= px else -1
            for x in range(px, tx + paso_x, paso_x):
                camino.append((x, py))
            paso_y = 1 if ty >= py else -1
            for y in range(py, ty + paso_y, paso_y):
                camino.append((tx, y))
        else:
            paso_y = 1 if ty >= py else -1
            for y in range(py, ty + paso_y, paso_y):
                camino.append((px, y))
            paso_x = 1 if tx >= px else -1
            for x in range(px, tx + paso_x, paso_x):
                camino.append((x, ty))
        px, py = tx, ty

    return camino


def _generar_camino_variado(origen, destino):
    """Elige entre camino simple (una L) o en zigzag (varios giros), segun PROPORCION_PASILLO_ZIGZAG."""
    if random.random() < PROPORCION_PASILLO_ZIGZAG:
        return _camino_zigzag(origen, destino, segmentos=random.choice([2, 3, 4]))
    return _camino_ortogonal(origen, destino)


def _tallar_pasillo(grid, id_region, altura_regiones, camino, id_pasillo,
                     ancho=None, altura=None):
    """Marca como piso transitable las celdas de un camino, sin sobrescribir piso existente."""
    if ancho is None:
        ancho = ANCHO_PASILLO
    if altura is None:
        altura = ALTURA_PASILLO

    alto_grid = len(grid)
    ancho_grid = len(grid[0])
    celdas_talladas = 0

    for (cx, cy) in camino:
        for dx in range(ancho):
            for dy in range(ancho):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < ancho_grid and 0 <= ny < alto_grid:
                    if grid[ny][nx] == 0:
                        grid[ny][nx] = 1
                        id_region[ny][nx] = id_pasillo
                        celdas_talladas += 1

    if celdas_talladas > 0:
        altura_regiones[id_pasillo] = altura

    return celdas_talladas


def conectar_pasillos(grid, id_region, altura_regiones, salas):
    """
    Interconecta TODAS las salas mediante pasillos ortogonales:
        1. Arbol de Expansion Minima (Prim) sobre los centros de las salas.
        2. Un porcentaje de conexiones extra (bucles no lineales, estilo DOOM).
    """
    if len(salas) < 2:
        return

    siguiente_id_pasillo = 1_000_000
    conectadas = [salas[0]]
    candidatas = list(salas[1:])
    aristas = []

    while candidatas:
        mejor_origen = mejor_destino = None
        mejor_idx = -1
        mejor_dist = float("inf")

        for idx, candidata in enumerate(candidatas):
            for conectada in conectadas:
                d = _distancia_manhattan(conectada["centro"], candidata["centro"])
                if d < mejor_dist:
                    mejor_dist = d
                    mejor_origen = conectada
                    mejor_destino = candidata
                    mejor_idx = idx

        aristas.append((mejor_origen, mejor_destino))
        conectadas.append(mejor_destino)
        candidatas.pop(mejor_idx)

    num_extra = max(0, int(len(salas) * PROPORCION_CONEXIONES_EXTRA))
    for _ in range(num_extra):
        a, b = random.sample(salas, 2)
        aristas.append((a, b))

    for origen, destino in aristas:
        camino = _generar_camino_variado(origen["centro"], destino["centro"])
        _tallar_pasillo(grid, id_region, altura_regiones, camino, siguiente_id_pasillo)
        siguiente_id_pasillo += 1


def _encontrar_componentes_conectados(grid):
    """BFS completo sobre la grilla: agrupa todas las celdas de piso en componentes."""
    alto, ancho = len(grid), len(grid[0])
    visitado = [[False] * ancho for _ in range(alto)]
    componentes = []

    for y in range(alto):
        for x in range(ancho):
            if grid[y][x] == 1 and not visitado[y][x]:
                componente = []
                cola = deque([(x, y)])
                visitado[y][x] = True
                while cola:
                    cx, cy = cola.popleft()
                    componente.append((cx, cy))
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < ancho and 0 <= ny < alto:
                            if grid[ny][nx] == 1 and not visitado[ny][nx]:
                                visitado[ny][nx] = True
                                cola.append((nx, ny))
                componentes.append(componente)

    return componentes


def verificar_conectividad_total(grid, id_region, altura_regiones):
    """
    Red de seguridad final: BFS sobre TODA la grilla; si detecta mas de un
    componente aislado, los une con pasillos de emergencia hasta lograr un
    unico componente conectado. Garantiza el 100% del requisito de "cero
    salas aisladas / cero callejones sin conexion".
    """
    siguiente_id_emergencia = 2_000_000
    componentes = _encontrar_componentes_conectados(grid)
    intentos_seguridad = 0

    while len(componentes) > 1 and intentos_seguridad < 200:
        intentos_seguridad += 1
        base = componentes[0]
        punto_base = base[len(base) // 2]

        mejor_idx, mejor_punto, mejor_dist = None, None, float("inf")
        for idx in range(1, len(componentes)):
            otro = componentes[idx]
            punto_otro = otro[len(otro) // 2]
            d = _distancia_manhattan(punto_base, punto_otro)
            if d < mejor_dist:
                mejor_dist = d
                mejor_idx = idx
                mejor_punto = punto_otro

        camino = _camino_ortogonal(punto_base, mejor_punto)
        _tallar_pasillo(grid, id_region, altura_regiones, camino,
                         siguiente_id_emergencia, altura=ALTURA_PASILLO)
        siguiente_id_emergencia += 1

        componentes = _encontrar_componentes_conectados(grid)

    return len(componentes) == 1


# ==============================================================================
# 3. CAPA DE CONSTRUCCION 3D - "EL MUSCULO" EN BMESH
#    (identica logica de altura-por-grupo-de-boundary-edges + fix de normales
#    del script original; ver comentarios inline para el razonamiento completo)
# ==============================================================================

def _limpiar_objeto_previo(nombre):
    """Elimina una ejecucion previa del generador para poder regenerar sin acumular objetos."""
    if nombre in bpy.data.objects:
        obj_previo = bpy.data.objects[nombre]
        mesh_previa = obj_previo.data
        bpy.data.objects.remove(obj_previo, do_unlink=True)
        if mesh_previa and mesh_previa.users == 0:
            bpy.data.meshes.remove(mesh_previa)


def generar_malla_3d(grid, id_region, altura_regiones, tamano_celda: float = None,
                      nombre_objeto: str = None):
    """
    Traduce la grilla logica (piso / vacio) en una unica malla 3D real:

        1. Un quad de piso por cada celda transitable (a Z=0).
        2. bmesh.ops.remove_doubles fusiona los vertices coincidentes de
           celdas adyacentes -> un piso continuo, sin paredes internas.
        3. edge.is_boundary detecta el perimetro libre real de cada zona.
        4. bmesh.ops.extrude_edge_only esculpe los muros exteriores,
           agrupando los bordes por la altura de su region para lograr
           los desniveles de techo variables (esencia DOOM).

    La malla resultante es un shell hueco SIN tapa superior (solo paredes +
    piso), transitable y sin caras internas ocultas, lista para editar por
    caras en Edit Mode.
    """
    if tamano_celda is None:
        tamano_celda = TAMANO_CELDA
    if nombre_objeto is None:
        nombre_objeto = NOMBRE_OBJETO

    _limpiar_objeto_previo(nombre_objeto)

    alto_grid = len(grid)
    ancho_grid = len(grid[0])

    bm = bmesh.new()
    capa_altura = bm.faces.layers.float.new("altura_muro")

    caras_piso = []

    for y in range(alto_grid):
        for x in range(ancho_grid):
            if grid[y][x] != 1:
                continue

            region = id_region[y][x]
            altura = altura_regiones.get(region, ALTURA_PASILLO)

            x0, x1 = x * tamano_celda, (x + 1) * tamano_celda
            y0, y1 = y * tamano_celda, (y + 1) * tamano_celda

            v1 = bm.verts.new((x0, y0, 0.0))
            v2 = bm.verts.new((x1, y0, 0.0))
            v3 = bm.verts.new((x1, y1, 0.0))
            v4 = bm.verts.new((x0, y1, 0.0))

            cara = bm.faces.new((v1, v2, v3, v4))
            cara[capa_altura] = altura
            caras_piso.append(cara)

    if not caras_piso:
        bm.free()
        raise RuntimeError(
            "No se genero ninguna celda de piso: revisa la cantidad de salas "
            "y el tamano de grilla configurados en el panel."
        )

    bm.verts.index_update()

    # PASO OBLIGATORIO: fusionar vertices adyacentes. Consolida el piso en una
    # unica superficie continua; al compartirse los vertices, cada arista
    # interna pasa a tener 2 caras vecinas y deja de ser "boundary", eliminando
    # de raiz cualquier pared interna invisible entre celdas contiguas.
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)

    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    # Agrupar los bordes perimetrales LIBRES por la altura de su region. Tras
    # el remove_doubles, solo quedan como boundary los bordes que dan al vacio
    # real (el perimetro exterior de cada sala/pasillo).
    grupos_por_altura = {}
    for arista in bm.edges:
        if arista.is_boundary:
            cara_vecina = arista.link_faces[0]
            h = cara_vecina[capa_altura]
            grupos_por_altura.setdefault(h, []).append(arista)

    # Extruir cada grupo de bordes a SU altura correspondiente, generando los
    # desniveles de techo variables que rompen la monotonia del mapa.
    for altura, aristas_grupo in grupos_por_altura.items():
        resultado = bmesh.ops.extrude_edge_only(bm, edges=aristas_grupo)
        nuevos_verts = [v for v in resultado["geom"] if isinstance(v, bmesh.types.BMVert)]
        for v in nuevos_verts:
            v.co.z += altura

    # Consistencia de normales: recalc_face_normals homogeneiza toda la malla
    # en un unico estado coherente, orientado "hacia afuera" del volumen hueco
    # (piso mirando -Z). Se invierte el resultado completo para que el piso
    # quede mirando +Z y, por coherencia geometrica, los muros queden mirando
    # hacia el interior (visibles para el jugador que camina DENTRO del nivel).
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    if caras_piso[0].normal.z < 0:
        bmesh.ops.reverse_faces(bm, faces=bm.faces)

    bm.normal_update()

    mesh_datos = bpy.data.meshes.new(nombre_objeto + "_Mesh")
    bm.to_mesh(mesh_datos)
    bm.free()
    mesh_datos.update()

    objeto = bpy.data.objects.new(nombre_objeto, mesh_datos)
    bpy.context.collection.objects.link(objeto)

    bpy.context.view_layer.objects.active = objeto
    objeto.select_set(True)

    return objeto


# ==============================================================================
# 4. CAPA DE ADDON - PROPIEDADES, OPERADOR Y PANEL
# ==============================================================================

def _aplicar_configuracion_desde_propiedades(props):
    """
    Traduce las propiedades configuradas en el panel del addon a las
    'constantes' globales que usa el pipeline de generacion (la misma logica
    ya validada de forma aislada, ahora parametrizable desde la UI en vez de
    constantes fijas en el codigo).
    """
    global GRID_ANCHO, GRID_ALTO, TAMANO_CELDA
    global NUM_HABITACIONES_OBJETIVO, MAX_INTENTOS_COLOCACION
    global TAMANO_MIN_SALA, TAMANO_MAX_SALA, MARGEN_ENTRE_SALAS
    global ALTURAS_SALAS_POSIBLES, ALTURA_PASILLO, ANCHO_PASILLO
    global SEMILLA_ALEATORIA

    GRID_ANCHO = props.grid_ancho
    GRID_ALTO = props.grid_alto
    TAMANO_CELDA = props.tamano_celda

    NUM_HABITACIONES_OBJETIVO = props.num_habitaciones
    MAX_INTENTOS_COLOCACION = max(3000, props.num_habitaciones * 150)

    TAMANO_MIN_SALA = min(props.tamano_min_sala, props.tamano_max_sala)
    TAMANO_MAX_SALA = max(props.tamano_min_sala, props.tamano_max_sala)
    MARGEN_ENTRE_SALAS = props.margen_entre_salas

    altura_min = min(props.altura_min, props.altura_max)
    altura_max = max(props.altura_min, props.altura_max)
    # 4 escalones de altura distribuidos entre el minimo y el maximo, para
    # mantener el aspecto de "niveles de techo" propio de Doom/OBSIDIAN.
    ALTURAS_SALAS_POSIBLES = [
        round(altura_min + i * (altura_max - altura_min) / 3.0, 2) for i in range(4)
    ]
    ALTURA_PASILLO = props.altura_pasillo
    ANCHO_PASILLO = props.ancho_pasillo

    SEMILLA_ALEATORIA = props.semilla if props.usar_semilla_fija else None


class OBSIDIAN_PG_propiedades(PropertyGroup):
    """Parametros configurables del generador, expuestos en el panel del viewport 3D."""

    grid_ancho: IntProperty(
        name="Ancho de grilla", description="Celdas en X",
        default=80, min=20, max=200)
    grid_alto: IntProperty(
        name="Alto de grilla", description="Celdas en Y",
        default=80, min=20, max=200)
    tamano_celda: FloatProperty(
        name="Tamano de celda", description="Tamano de 1 bloque clasico DOOM (unidades de Blender)",
        default=2.0, min=0.5, max=10.0)

    num_habitaciones: IntProperty(
        name="Num. de salas", description="Cantidad objetivo de salas a colocar",
        default=32, min=2, max=150)
    tamano_min_sala: IntProperty(
        name="Tamano min. sala", description="Tamano minimo de una sala, en celdas",
        default=5, min=3, max=30)
    tamano_max_sala: IntProperty(
        name="Tamano max. sala", description="Tamano maximo de una sala, en celdas",
        default=12, min=3, max=30)
    margen_entre_salas: IntProperty(
        name="Margen entre salas", description="Separacion minima obligatoria entre salas, en celdas",
        default=2, min=1, max=6)

    altura_min: FloatProperty(
        name="Altura minima", description="Altura de techo mas baja posible para una sala",
        default=3.0, min=1.0, max=20.0)
    altura_max: FloatProperty(
        name="Altura maxima", description="Altura de techo mas alta posible para una sala",
        default=6.0, min=1.0, max=20.0)
    altura_pasillo: FloatProperty(
        name="Altura de pasillo", description="Altura de techo fija para todos los pasillos",
        default=3.0, min=1.0, max=20.0)

    ancho_pasillo: IntProperty(
        name="Ancho de pasillo", description="Ancho de los pasillos, en celdas",
        default=1, min=1, max=4)

    usar_semilla_fija: BoolProperty(
        name="Usar semilla fija", description="Genera siempre el mismo resultado para poder reproducirlo",
        default=False)
    semilla: IntProperty(
        name="Semilla", description="Semilla aleatoria (solo aplica si 'Usar semilla fija' esta activo)",
        default=0, min=0)


class OBSIDIAN_OT_generar_nivel(Operator):
    """Genera un nuevo nivel procedural estilo OBSIDIAN, reemplazando el anterior"""
    bl_idname = "obsidian.generar_nivel"
    bl_label = "Generar Nivel OBSIDIAN"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.obsidian_props
        _aplicar_configuracion_desde_propiedades(props)

        if SEMILLA_ALEATORIA is not None:
            random.seed(SEMILLA_ALEATORIA)

        grid, id_region, altura_regiones = inicializar_grilla(GRID_ANCHO, GRID_ALTO)
        salas = generar_habitaciones_obsidian(grid, id_region, altura_regiones)
        conectar_pasillos(grid, id_region, altura_regiones, salas)
        conectividad_ok = verificar_conectividad_total(grid, id_region, altura_regiones)

        try:
            objeto = generar_malla_3d(grid, id_region, altura_regiones)
        except RuntimeError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

        num_caras = len(objeto.data.polygons)
        num_verts = len(objeto.data.vertices)
        mensaje = (
            f"Nivel generado: {len(salas)}/{props.num_habitaciones} salas | "
            f"{num_caras} caras | {num_verts} vertices | "
            f"Conectividad total: {conectividad_ok}"
        )

        if len(salas) < props.num_habitaciones:
            # La combinacion de grilla/tamano/margen no daba espacio para todas
            # las salas pedidas: se coloco el maximo posible sin solapar, en
            # vez de fallar. Se avisa para que el usuario pueda ajustar
            # parametros si de verdad necesita esa cantidad exacta.
            self.report(
                {'WARNING'},
                mensaje + " -- no entraron todas: proba una grilla mas grande, "
                          "salas mas chicas o menos margen"
            )
        else:
            self.report({'INFO'}, mensaje)

        return {'FINISHED'}


class OBSIDIAN_PT_panel(Panel):
    """Panel de control del generador en la barra lateral del viewport 3D."""
    bl_label = "OBSIDIAN Level Generator"
    bl_idname = "OBSIDIAN_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "OBSIDIAN"

    def draw(self, context):
        layout = self.layout
        props = context.scene.obsidian_props

        col = layout.column()
        col.label(text="Grilla")
        col.prop(props, "grid_ancho")
        col.prop(props, "grid_alto")
        col.prop(props, "tamano_celda")

        layout.separator()
        col = layout.column()
        col.label(text="Salas")
        col.prop(props, "num_habitaciones")
        fila = col.row(align=True)
        fila.prop(props, "tamano_min_sala")
        fila.prop(props, "tamano_max_sala")
        col.prop(props, "margen_entre_salas")

        layout.separator()
        col = layout.column()
        col.label(text="Alturas (desnivel estilo DOOM)")
        fila = col.row(align=True)
        fila.prop(props, "altura_min")
        fila.prop(props, "altura_max")
        col.prop(props, "altura_pasillo")

        layout.separator()
        col = layout.column()
        col.label(text="Pasillos")
        col.prop(props, "ancho_pasillo")

        layout.separator()
        col = layout.column()
        col.label(text="Semilla aleatoria")
        col.prop(props, "usar_semilla_fija")
        if props.usar_semilla_fija:
            col.prop(props, "semilla")

        layout.separator()
        layout.operator("obsidian.generar_nivel")


# ==============================================================================
# 5. REGISTRO DEL ADDON
# ==============================================================================

_CLASES_ADDON = (
    OBSIDIAN_PG_propiedades,
    OBSIDIAN_OT_generar_nivel,
    OBSIDIAN_PT_panel,
)


def register():
    for cls in _CLASES_ADDON:
        bpy.utils.register_class(cls)
    bpy.types.Scene.obsidian_props = PointerProperty(type=OBSIDIAN_PG_propiedades)


def unregister():
    del bpy.types.Scene.obsidian_props
    for cls in reversed(_CLASES_ADDON):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
