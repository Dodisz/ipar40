# Ipar 4.0 – Prediktív karbantartás kiberfizikai szimulációval

Házi feladat. A projekt egy forgó gép (pl. motor, csapágy) **rezgés- és áramjelét** szimulálja,
és **MQTT** alapú publish/subscribe architektúrán keresztül egy peremhálózati (edge) csomópontnak
továbbítja. Az edge csomópont két különböző detektorral – egy **szűretlen** és egy **szűrt** –
próbálja meg időben felismerni a gép kopását, mielőtt az nem tervezett leálláshoz vezetne.

A projekt célja annak bemutatása, hogy megfelelő jelfeldolgozással (szűrés, megerősítés,
hiszterézis) jelentősen csökkenthető a téves riasztások száma, és ezzel a karbantartás várható
költsége.

---

## Tartalom

- [Architektúra](#architektúra)
- [Követelmények](#követelmények)
- [Telepítés](#telepítés)
- [Futtatási módok](#futtatási-módok)
- [A rendszer működése](#a-rendszer-működése)
  - [1. Fizikai réteg – szimulátor](#1-fizikai-réteg--szimulátor)
  - [2. Peremhálózati logika – detektorok](#2-peremhálózati-logika--detektorok)
  - [3. Kiértékelés és költségmodell](#3-kiértékelés-és-költségmodell)
  - [4. Kommunikáció – MQTT](#4-kommunikáció--mqtt)
  - [5. Verifikáció – tesztesetek](#5-verifikáció--tesztesetek)
- [Konfigurálható paraméterek](#konfigurálható-paraméterek)
- [Eredmények](#eredmények)
- [Kimeneti fájlok](#kimeneti-fájlok)

---

## Architektúra

```
[sim]  IndustrialSimulator --MQTT--> ipar40/potha/sensor_data
                                              |
                             helyi broker (Mosquitto, localhost:1883)
                                              |
[edge] EdgeNode (feliratkozó) <---------------+
       - szűretlen detektor  (minden mintát a küszöbhöz hasonlít)
       - szűrt detektor      (mozgó medián -> mozgó RMS -> megerősítés + hiszterézis)
       --MQTT--> ipar40/potha/filtered   (szűrt adatfolyam)
       --MQTT--> ipar40/potha/alarm      (riasztási események)
```

| Topic                       | Irány           | Tartalom                                              |
|-----------------------------|-----------------|-------------------------------------------------------|
| `ipar40/potha/sensor_data`  | sim → edge      | nyers szenzoradat (időbélyeg, lépés, rezgés, áram)    |
| `ipar40/potha/filtered`     | edge → bárki    | a szűrt detektor RMS-értéke és riasztási állapota     |
| `ipar40/potha/alarm`        | edge → bárki    | riasztási események (detektor, lépés, jellemző, küszöb, időbélyegek) |

Minden üzenet JSON formátumú, és **QoS 1** szinttel kerül továbbításra.

---

## Követelmények

- **Python 3.10+** (a kód a `int | None` típusjelölést használja)
- `edge` és `sim` módhoz:
  - [`paho-mqtt`](https://pypi.org/project/paho-mqtt/) (1.x és 2.x verzióval is működik)
  - egy futó MQTT broker, pl. [Mosquitto](https://mosquitto.org/)
- `analyze --plot` módhoz (opcionális): `matplotlib`
- Az `analyze` és `test` mód csak a Python standard könyvtárát használja, broker nem kell hozzá.

## Telepítés

```bash
git clone https://github.com/Dodisz/ipar40.git
cd ipar40
pip install -r requirements.txt
```

Mosquitto broker indítása (külön terminálban):

```bash
mosquitto -v
```

---

## Futtatási módok

A program egyetlen fájl (`Ipar4.0.py`), a működési módot az első argumentum választja ki.

### `edge` – feliratkozó / peremhálózati logika

```bash
python Ipar4.0.py edge [--broker localhost] [--port 1883] [--log edge_log.csv]
```

Feliratkozik a szenzoradatokra, mindkét detektorral feldolgozza őket, publikálja a szűrt
adatfolyamot és a riasztásokat, és CSV-be naplózza az összes mintát. A szimulátor
`{"end": true}` üzenetére (vagy Ctrl+C-re) összegzést ír ki.

### `sim` – szenzorszimulátor (publikáló)

```bash
python Ipar4.0.py sim [--broker localhost] [--port 1883] [--interval 0.1] [--onset 150] [--steps 0] [--seed 42]
```

| Kapcsoló     | Alapérték | Jelentés                                                    |
|--------------|-----------|-------------------------------------------------------------|
| `--interval` | `0.1`     | mintavételi idő másodpercben                                |
| `--onset`    | `150`     | a kopás kezdete (lépésszám)                                 |
| `--steps`    | `0`       | szimulált lépések száma; `0` = a meghibásodásig + 20 lépés  |
| `--seed`     | `42`      | véletlenszám-generátor magja (reprodukálható futás)         |

**Tipikus használat:** először az `edge`-et kell elindítani az egyik terminálban, majd a
`sim`-et a másikban.

### `analyze` – küszöb-érzékenységvizsgálat (MQTT nélkül)

```bash
python Ipar4.0.py analyze [--runs 200] [--csv eredmeny.csv] [--plot]
```

Monte Carlo módszerrel sok (alapból 200) véletlenszerű gépéletciklust szimulál minden vizsgált
küszöbértékre, mindkét detektorral, és kiszámolja a téves riasztási arányt, az átlagos
válaszidőt, a nem tervezett leállások arányát és a várható költséget.

| Kapcsoló  | Jelentés                                                                 |
|-----------|--------------------------------------------------------------------------|
| `--runs`  | gépéletciklusok száma küszöbönként                                      |
| `--csv`   | az eredménytábla mentése CSV-fájlba                                     |
| `--plot`  | grafikon: válaszidő–téves riasztás kompromisszum és küszöb–költség görbe |

### `test` – verifikációs tesztesetek

```bash
python Ipar4.0.py test
```

---

## A rendszer működése

### 1. Fizikai réteg – szimulátor

Az `IndustrialSimulator` osztály lépésenként egy mintát állít elő:

- **Kopás (`wear`)**: a `wear_onset` lépésig a gép ép (`wear = 0`), utána a kopás lineárisan nő,
  és `wear_duration` (alapból 200) lépés alatt eléri az 1.0 értéket – ez a **meghibásodás**
  (nem tervezett leállás) pillanata.
- **Rezgésgyorsulás**:
  - alapzaj: normális eloszlás, várható érték 0,5, szórás 0,08;
  - kopásból eredő harmonikus összetevő (kiegyensúlyozatlanság): `wear · 1,8 · sin(0,6 · lépés)`;
  - kopásból eredő érdesség: `wear · U(0; 0,5)`;
  - **külső ütés**: `shock_prob` (alapból 3%) valószínűséggel egy ±1,5…3,0 amplitúdójú
    impulzus. Ez **nem hiba**, de a szűretlen detektornál téves riasztást okoz.
- **Motoráram**: `4,2 + N(0; 0,04) + 0,9 · wear` – a mechanikai ellenállás miatt a kopással nő.

A kimenetben szerepel a `wear_true` és a `shock_true` mező is. Ezek **csak a kiértékeléshez**
(ground truth) kellenek, a detektorok nem használják őket.

### 2. Peremhálózati logika – detektorok

**Szűretlen detektor (`RawDetector`)**
Minden egyes minta abszolút értékét a küszöbhöz (alapból 2,0) hasonlítja. Egyszerű és gyors,
de minden külső ütésre riaszt.

**Szűrt detektor (`FilteredDetector`)** – négylépéses feldolgozási lánc:

1. **Mozgó medián** (5 mintás ablak): kiszűri a rövid impulzuszajt (ütéseket).
2. **Mozgó RMS** (21 mintás ablak): a rezgés energiáját jellemzi; inkrementálisan számolódik,
   tehát nem kell minden lépésben az egész ablakot újraszámolni. Amíg az ablak nem telik meg
   (bemelegedés), a detektor nem ad kimenetet.
3. **Megerősítés**: csak akkor riaszt, ha az RMS legalább 2 egymást követő lépésben a küszöb
   (alapból 0,8) felett van.
4. **Hiszterézis**: a riasztás csak akkor szűnik meg, ha az RMS a küszöb 90%-a alá esik. Ez
   megakadályozza a riasztás „pattogását” a küszöb körül.

Riasztási **eseménynek** a felfutó él számít (normál → riasztás átmenet), nem minden riasztó
minta külön.

### 3. Kiértékelés és költségmodell

Egy gépéletciklus a meghibásodásig tart. A kiértékelés során:

- az ép gépen (`wear = 0`) kiváltott riasztás **téves riasztás**;
- az első, kopás közbeni riasztás a **valódi észlelés**, ekkor a gépet karbantartásra küldik;
- **válaszidő (késés)** = a kopás kezdetétől az észlelésig eltelt lépések száma;
- **időben észlelt** az eset, ha az észleléstől a meghibásodásig legalább `MIN_LEAD_TIME`
  (30) lépés marad a karbantartás megszervezésére.

| Esemény                                       | Költség (tetszőleges egység, pl. EUR) |
|-----------------------------------------------|---------------------------------------|
| téves riasztás (felesleges kiszállás)         | 500                                   |
| tervezett karbantartás (időben észlelt kopás) | 2 000                                 |
| nem tervezett leállás (késői / elmaradt észlelés) | 20 000                            |

Egy gép költsége = `téves riasztások száma · 500 + (2 000 vagy 20 000)`.

A Monte Carlo vizsgálat (`sweep`) minden küszöbhöz **ugyanazokat** a véletlen gépéletciklusokat
használja (azonos seedek), így a küszöbértékek összehasonlítása igazságos. A kopás kezdete
futásonként véletlenszerűen 100 és 400 lépés közé esik.

### 4. Kommunikáció – MQTT

- A `make_client` a `paho-mqtt` 1.x és 2.x API-jával is kompatibilis klienst hoz létre.
- A **szimulátor** minden mintát JSON-ként publikál a `sensor_data` topicra, a végén egy
  `{"end": true}` lezáró üzenetet küld.
- Az **`EdgeNode`** minden beérkező üzenetnél:
  - ellenőrzi a lépésszámot → **elveszett** és **duplikált** üzenetek számlálása;
  - méri az **átviteli késleltetést** (küldési és fogadási időbélyeg különbsége) és a
    **feldolgozási időt**;
  - mindkét detektort frissíti; új riasztási eseménynél üzenetet küld az `alarm` topicra;
  - a szűrt RMS-t publikálja a `filtered` topicra;
  - minden mintát a CSV-naplóba ír.
- A futás végén összegzést ad: üzenetvesztés, duplikátumok, riasztási események detektoronként
  (ebből hány téves, mikor volt az első valódi észlelés), valamint a késleltetés átlaga,
  95. percentilise és maximuma.

### 5. Verifikáció – tesztesetek

A `VerificationTests` osztály 9 egységtesztet tartalmaz:

| Teszt                                     | Mit ellenőriz                                                              |
|-------------------------------------------|----------------------------------------------------------------------------|
| `test_simulator_wear_profile`             | a kopás a kezdetig 0, utána monoton nő, és a várt lépésben éri el az 1.0-t |
| `test_simulator_reproducible`             | azonos seed esetén azonos a jelsorozat                                     |
| `test_current_rises_with_wear`            | a motoráram a kopással érdemben (> 0,7) nő                                 |
| `test_rms_of_constant_signal`             | konstans 1,0 jel RMS-e 1,0                                                 |
| `test_single_shock_raw_alarms_filtered_not` | egyetlen ütésre a szűretlen detektor riaszt, a szűrt nem                 |
| `test_bounded_response_time`              | ugrásszerű változásra a szűrt detektor korlátos időn belül reagál          |
| `test_hysteresis_prevents_chattering`     | a hiszterézissávban a riasztás megmarad, alatta megszűnik                  |
| `test_cost_model`                         | a költségmodell helyesen számol elmaradt, késői és időben történt észlelésnél |
| `test_filtered_beats_raw_on_cost`         | optimális küszöbön a szűrt detektor várható költsége kisebb               |

---

## Konfigurálható paraméterek

A fájl elején található konstansok:

| Konstans             | Érték         | Jelentés                                       |
|----------------------|---------------|------------------------------------------------|
| `BROKER`, `PORT`     | `localhost`, `1883` | MQTT broker címe (teszteléshez pl. `broker.hivemq.com`) |
| `TOPIC_*`            | lásd fent     | MQTT topicok                                   |
| `COST_FALSE_ALARM`   | 500           | téves riasztás költsége                        |
| `COST_PLANNED`       | 2 000         | tervezett karbantartás költsége                |
| `COST_UNPLANNED`     | 20 000        | nem tervezett leállás költsége                 |
| `MIN_LEAD_TIME`      | 30            | a karbantartás megszervezéséhez szükséges lépések |
| `RAW_THRESHOLD`      | 2,0           | a szűretlen detektor küszöbe                   |
| `FILTERED_THRESHOLD` | 0,8           | a szűrt detektor (RMS) küszöbe                 |
| `MEDIAN_WINDOW`      | 5             | mozgó medián ablakmérete                       |
| `RMS_WINDOW`         | 21            | mozgó RMS ablakmérete                          |
| `CONFIRM_STEPS`      | 2             | megerősítéshez szükséges egymást követő túllépések |
| `HYSTERESIS`         | 0,9           | visszaállási szint a küszöb arányában          |

---

## Eredmények

Az `analyze` mód kimenete alapértelmezett beállításokkal (200 gépéletciklus / küszöb):

| Detektor  | Optimális küszöb | Téves riasztás / 1000 lépés | Átlagos késés [lépés] | Nem tervezett leállás | Várható költség / gép |
|-----------|------------------|-----------------------------|-----------------------|-----------------------|-----------------------|
| szűretlen | 2,0              | 19,56                       | 51,0                  | 0%                    | 4 505                 |
| szűrt     | 0,8              | 0,00                        | 105,7                 | 0%                    | **2 000**             |

**Értelmezés:**

- A szűretlen detektor gyorsabban reagál, de a külső ütések miatt gépenként átlagosan több téves
  riasztást ad, ezért a várható költsége több mint kétszerese a szűrt detektorénak.
- A szűrt detektor később észleli a kopást, de még bőven a meghibásodás előtt, így minden
  esetben tervezett karbantartás történik, téves riasztás nélkül.
- A küszöb túl magasra állítása mindkét detektornál gyorsan a nem tervezett leállások arányának
  meredek növekedéséhez vezet (pl. szűrt detektornál 1,1-es küszöbön már 39%) – a küszöbválasztás
  tehát a **válaszidő és a téves riasztások közötti kompromisszum**.

---

## Kimeneti fájlok

- **`edge_log.csv`** (az `edge` mód hozza létre, a név a `--log` kapcsolóval módosítható):
  mintánként egy sor a következő oszlopokkal: `step`, `t_sent`, `t_recv`, `latency_ms`,
  `proc_ms`, `vibration`, `current`, `wear_true`, `raw_feature`, `raw_alarm`, `rms`, `filt_alarm`.
- **`--csv` fájl** (az `analyze` mód hozza létre kérésre): küszöbönként és detektoronként a
  téves riasztási arány, átlagos késés, leállási arány és átlagos költség.
