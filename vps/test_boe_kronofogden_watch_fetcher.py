#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("boe_kronofogden_watch_fetcher.py")
SPEC = importlib.util.spec_from_file_location("boe_krono_watch", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)


BOE_SEARCH = """
<ul>
 <li><a href="./detalleSubasta.php?idSub=SUB-JA-2026-263728&amp;idBus=x">one</a></li>
 <li><a href="detalleSubasta.php?idSub=SUB-AT-2026-24R4186002046">two</a></li>
 <li><a href="detalleSubasta.php?idSub=not-an-auction">bad</a></li>
</ul>
"""

BOE_GENERAL = """
<table>
 <tr><th>Fecha de conclusión</th><td>24-08-2026 18:00:00 CET
 (ISO: 2026-08-24T18:00:00+02:00)</td></tr>
 <tr><th>Lotes</th><td>2</td></tr>
</table>
"""

BOE_LOT = """
<div id="cont-tabs">
 <a id="idTabLote1">Lote 1</a><a id="idTabLote2">Lote 2</a>
</div>
<table>
 <tr><th>Valor Subasta</th><td>12.500,00 €</td></tr>
 <tr><th>Puja mínima</th><td>1.250,00 €</td></tr>
</table>
<h4>Bien 1 - Vehículo (Turismos)</h4>
<table>
 <tr><th>Descripción</th><td>PEUGEOT 208 GASOLINA, 54.321 km</td></tr>
 <tr><th>Matrícula</th><td>1234ABC</td></tr>
 <tr><th>Marca</th><td>PEUGEOT</td></tr>
 <tr><th>Modelo</th><td>208</td></tr>
 <tr><th>Fecha de matriculación</th><td>02-05-2024</td></tr>
</table>
"""

BOE_HIDDEN_BIDS = """
<h3>Pujas máximas actuales</h3><table>
 <tr><th>Lote</th><th>Importe de la puja</th></tr>
 <tr><td>1</td><td>Con puja (inicie sesión para consultar el importe)</td></tr>
</table>
"""

BOE_PUBLIC_BIDS = """
<h3>Pujas máximas actuales</h3><table>
 <tr><th>Lote</th><th>Importe de la puja</th></tr>
 <tr><td>1</td><td>13.750,00 €</td></tr>
</table>
"""

KRONO_LIST = """
<div class="obj_list_speed_container grid">
 <div id="115355" class="obj_thumbnail">
  <a class="obj_link" href="w.object?inC=KFM&amp;inA=20260804_1443&amp;inO=1">
   <div class="obj_txt_inner"><span class="text-muted">F108033.</span> Kia Niro<br>
   Malmö<br>Utrop 120 000 SEK</div>
  </a>
 </div>
</div>
"""

KRONO_MATRIX = """
var Matrix=new Array();
Matrix[0]=['115355','50 000 SEK','50000','3','13','15','9','46 000 SEK','20260804_1443','0','0','','',''];
Matrix[1]=['115356','0','0','3','13','16','9','30 000 SEK','20260804_1443','0','0','','',''];
"""

KRONO_DETAIL = """
<h1><span class="text-muted">F108033. </span>Kia Niro</h1>
<div>Registreringsnummer ABC123<br>
Drivmedel bensin/el<br>
Avläst mätarställning 12 345 mil<br>
Första gången i trafik 2024-04-03<br></div>
<div id="bid_list_container_115355">
 <h4><span class="obj_time_txt">3 dagar</span><br>
 <small>25 Augusti 2026 10:29</small></h4>
 <h4>Budgivning</h4><table><tr><td>50 000 SEK</td></tr></table>
</div>
"""


class WatchParserTests(unittest.TestCase):
    def test_boe_search_general_and_lot_numbers(self):
        self.assertEqual(len(module.parse_boe_search(BOE_SEARCH)), 2)
        general = module.parse_boe_general(BOE_GENERAL)
        self.assertEqual(general["end"], "2026-08-24T18:00:00+02:00")
        self.assertEqual(general["lot_count"], 2)
        self.assertEqual(module.parse_boe_lot_numbers(BOE_LOT), [1, 2])

    def test_boe_hidden_current_bid_falls_back_to_labelled_base(self):
        bids, hidden = module.parse_boe_bid_page(BOE_HIDDEN_BIDS)
        row = module.parse_boe_vehicle_lot(
            BOE_LOT, auction_id="SUB-JA-2026-263728", lot_number=1,
            general=module.parse_boe_general(BOE_GENERAL), public_bids=bids,
            hidden_bid=hidden, observed_at="2026-08-21T20:00:00Z",
        )
        self.assertIsNotNone(row)
        self.assertEqual(set(row), set(module.WATCH_FIELDNAMES))
        self.assertEqual(row["price_amount"], "12500")
        self.assertEqual(row["price_kind"], "base_price")
        self.assertEqual(row["bid_visibility"], "login_required_current_bid")
        self.assertEqual(row["registration_date"], "2024-05-02")
        self.assertEqual(row["fuel"], "petrol")
        self.assertEqual(row["mileage_km"], "54321")
        self.assertEqual(row["eligibility_status"], "conditional")
        self.assertEqual(row["source_key"], "boe-subastas")
        self.assertEqual(row["last_seen_at"], "2026-08-21T20:00:00Z")

    def test_boe_public_numeric_bid_wins_over_base(self):
        bids, hidden = module.parse_boe_bid_page(BOE_PUBLIC_BIDS)
        row = module.parse_boe_vehicle_lot(
            BOE_LOT, auction_id="SUB-JA-2026-263728", lot_number=1,
            general=module.parse_boe_general(BOE_GENERAL), public_bids=bids,
            hidden_bid=hidden, observed_at="2026-08-21T20:00:00Z",
        )
        self.assertEqual(row["price_amount"], "13750")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["bid_visibility"], "public_numeric")

    def test_kronofogden_list_matrix_and_detail(self):
        items = module.parse_krono_list(KRONO_LIST)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["internal_id"], "115355")
        matrix = module.parse_krono_live_js(KRONO_MATRIX)
        self.assertEqual(matrix["115355"][2], "50000")
        row = module.parse_krono_detail(
            KRONO_DETAIL, item=items[0], live=matrix["115355"],
            observed_at="2026-08-21T20:00:00Z", rates={"SEK": 10.0},
        )
        self.assertEqual(set(row), set(module.WATCH_FIELDNAMES))
        self.assertEqual(row["title"], "Kia Niro")
        self.assertEqual(row["registration_date"], "2024-04-03")
        self.assertEqual(row["year"], "2024")
        self.assertEqual(row["fuel"], "hybrid")
        self.assertEqual(row["mileage_km"], "123450")
        self.assertEqual(row["price_amount"], "50000")
        self.assertEqual(row["price_eur"], "5000")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["sale_end_at"], "2026-08-25T10:29:00+02:00")
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertEqual(row["source_key"], "kronofogden")
        self.assertEqual(row["last_seen_at"], "2026-08-21T20:00:00Z")

    def test_kronofogden_zero_bid_uses_base_price(self):
        item = module.parse_krono_list(KRONO_LIST)[0]
        live = module.parse_krono_live_js(KRONO_MATRIX)["115356"]
        row = module.parse_krono_detail(
            KRONO_DETAIL, item=item, live=live,
            observed_at="2026-08-21T20:00:00Z", rates={"SEK": 10.0},
        )
        self.assertEqual(row["price_amount"], "30000")
        self.assertEqual(row["price_kind"], "base_price")

    def test_top_level_schema(self):
        row = {field: "" for field in module.WATCH_FIELDNAMES}
        payload = module.build_payload(
            [row], {"generated_at": "2026-08-21T20:00:00Z", "sources": {"x": {}}}
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["lane"], "official_auction_watch")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["source_reports"], {"x": {}})


if __name__ == "__main__":
    unittest.main()
