import ezdxf
from shapely.geometry import Polygon, box
from shapely.affinity import translate, rotate
from shapely.ops import polylabel

# ==========================================
# НАЛАШТУВАННЯ ПАРАМЕТРІВ (в мм)
# ==========================================
SHEET_WIDTH = 1050   # Ширина листа
SHEET_HEIGHT = 895  # Висота листа
PADDING = 2          # Відступ між деталями та від країв
MAX_FONT_H = 13      # Максимальна висота шрифту
# ==========================================

def get_fitted_text_params(poly, text, max_font_h=MAX_FONT_H):
    """Знаходить найкращу точку і розмір шрифту всередині фігури."""
    try:
        best_point = polylabel(poly, tolerance=1.0)
    except:
        # Резервний варіант, якщо деталь занадто мала для polylabel
        rep = poly.representative_point()
        return (rep.x, rep.y), 1.0
        
    current_h = max_font_h
    num_chars = len(str(text))
    
    while current_h > 1.0:
        tw = current_h * num_chars * 0.7
        th = current_h
        text_bbox = box(best_point.x - tw/2, best_point.y - th/2, 
                        best_point.x + tw/2, best_point.y + th/2)
        
        if poly.contains(text_bbox):
            break
        current_h -= 0.5 # Більш точний підбір (крок 0.5мм)
        
    return (best_point.x, best_point.y), current_h

def get_grouped_polygons(filename):
    doc = ezdxf.readfile(filename)
    msp = doc.modelspace()
    all_polys = []
    for entity in msp.query('LWPOLYLINE'):
        if entity.closed:
            points = [(p[0], p[1]) for p in entity.get_points()]
            if len(points) >= 3:
                poly = Polygon(points)
                if poly.is_valid: all_polys.append(poly)
    
    all_polys.sort(key=lambda p: p.area, reverse=True)
    grouped_parts = []
    used_indices = set()
    
    for i in range(len(all_polys)):
        if i in used_indices: continue
        parent = all_polys[i]
        holes = []
        for j in range(i + 1, len(all_polys)):
            if j not in used_indices and parent.contains(all_polys[j]):
                holes.append(all_polys[j].exterior.coords)
                used_indices.add(j)
        
        minx, miny, _, _ = parent.bounds
        final_poly = Polygon(parent.exterior.coords, holes)
        
        grouped_parts.append({
            'poly': translate(final_poly, -minx, -miny),
            'id': len(grouped_parts) + 1,
            'orig_full_poly': final_poly
        })
        used_indices.add(i)
    return grouped_parts

def add_numbered_poly(msp, poly, part_id, x_offset=0, y_offset=0):
    """Малює контур та текст у шарі MARKING."""
    final_poly = translate(poly, x_offset, y_offset)
    msp.add_lwpolyline(list(final_poly.exterior.coords), dxfattribs={'closed': True})
    for hole in final_poly.interiors:
        msp.add_lwpolyline(list(hole.coords), dxfattribs={'closed': True})
    
    pos, font_h = get_fitted_text_params(poly, part_id)
    msp.add_text(str(part_id), 
                dxfattribs={'height': font_h, 'layer': 'MARKING', 'color': 1}
                ).set_placement((pos[0] + x_offset, pos[1] + y_offset), 
                                align=ezdxf.enums.TextEntityAlignment.CENTER)

def nest_logic(parts_data, sw=SHEET_WIDTH, sh=SHEET_HEIGHT, pad=PADDING):
    sheets = []
    current_sheet = []
    
    processed = []
    for p_d in parts_data:
        p = p_d['poly']
        p90 = rotate(p, 90, origin=(0,0))
        b0, b90 = p.bounds, p90.bounds
        w0, h0 = b0[2]-b0[0], b0[3]-b0[1]
        w90, h90 = b90[2]-b90[0], b90[3]-b90[1]

        # Перевірка: чи влізе деталь взагалі
        if not ((w0 <= sw-2*pad and h0 <= sh-2*pad) or (w90 <= sw-2*pad and h90 <= sh-2*pad)):
            print(f"!!! Деталь {p_d['id']} занадто велика для листа {sw}x{sh}!")
            continue

        # Вибір орієнтації для Shelf Packing (мінімізація висоти)
        if (h90 < h0 and w90 <= sw-2*pad) or (w0 > sw-2*pad):
            processed.append({'poly': translate(p90, -b90[0], -b90[1]), 'id': p_d['id']})
        else:
            processed.append({'poly': p, 'id': p_d['id']})

    processed.sort(key=lambda x: x['poly'].bounds[3] - x['poly'].bounds[1], reverse=True)
    
    cx, cy, sh_h = pad, pad, 0
    for item in processed:
        p = item['poly']
        w, h = p.bounds[2]-p.bounds[0], p.bounds[3]-p.bounds[1]
        
        if cx + w + pad > sw:
            cx, cy, sh_h = pad, cy + sh_h + pad, 0
        if cy + h + pad > sh:
            sheets.append(current_sheet)
            current_sheet, cx, cy, sh_h = [], pad, pad, 0
            
        current_sheet.append({'poly': translate(p, cx, cy), 'id': item['id']})
        cx += w + pad
        sh_h = max(sh_h, h)
        
    if current_sheet: sheets.append(current_sheet)
    return sheets

# --- ЗАПУСК ---
input_filename = "for_nesting.dxf"
parts = get_grouped_polygons(input_filename)

# 1. Вхідний файл
in_doc = ezdxf.new('R2010')
in_doc.layers.new(name='MARKING', dxfattribs={'color': 1})
for p in parts:
    add_numbered_poly(in_doc.modelspace(), p['orig_full_poly'], p['id'])
in_doc.saveas("for_nesting_numeric.dxf")

# 2. Нестинг
nested_result = nest_logic(parts)

# 3. Вихідний файл
out_doc = ezdxf.new('R2010')
out_doc.layers.new(name='MARKING', dxfattribs={'color': 1})
out_doc.layers.new(name='SHEET_BOUND', dxfattribs={'color': 4})

for i, sheet in enumerate(nested_result):
    x_off = i * (SHEET_WIDTH + 100)
    # Рамка листа за заданими розмірами
    out_doc.modelspace().add_lwpolyline(
        [(x_off, 0), (x_off + SHEET_WIDTH, 0), (x_off + SHEET_WIDTH, SHEET_HEIGHT), 
         (x_off, SHEET_HEIGHT), (x_off, 0)], 
        dxfattribs={'layer': 'SHEET_BOUND'}
    )
    for item in sheet:
        add_numbered_poly(out_doc.modelspace(), item['poly'], item['id'], x_offset=x_off)

out_doc.saveas("nested_for_cut.dxf")
print(f"Виконано! Використано листів ({SHEET_WIDTH}x{SHEET_HEIGHT}): {len(nested_result)}")