import argparse
import csv
import json
import math
import random
import statistics
import sys
import time
import unittest
from collections import deque
from dataclasses import dataclass


BROKER = "localhost"
PORT = 1883
TOPIC_SENSOR = "ipar40/potha/sensor_data"
TOPIC_FILTERED = "ipar40/potha/filtered"
TOPIC_ALARM = "ipar40/potha/alarm"


COST_FALSE_ALARM = 500
COST_PLANNED = 2_000
COST_UNPLANNED = 20_000
MIN_LEAD_TIME = 30


RAW_THRESHOLD = 2.0
FILTERED_THRESHOLD = 0.8
MEDIAN_WINDOW = 5
RMS_WINDOW = 21
CONFIRM_STEPS = 2
HYSTERESIS = 0.9


class IndustrialSimulator:
    def __init__(self, wear_onset=150, wear_duration=200, shock_prob=0.03, seed=None):
        self.rng = random.Random(seed)
        self.wear_onset = wear_onset
        self.wear_duration = wear_duration
        self.shock_prob = shock_prob
        self.step = 0
        self.wear = 0.0

    @property
    def failure_step(self):
        return self.wear_onset + self.wear_duration

    def generate(self):
        rng = self.rng
        self.step += 1
        if self.step > self.wear_onset:
            self.wear = min(1.0, (self.step - self.wear_onset) / self.wear_duration)

        vibration = rng.gauss(0.5, 0.08)
        vibration += self.wear * (1.8 * math.sin(self.step * 0.6) + rng.uniform(0, 0.5))

        shock = rng.random() < self.shock_prob
        if shock:
            vibration += rng.choice((-1, 1)) * rng.uniform(1.5, 3.0)

        current = 4.2 + rng.gauss(0, 0.04) + 0.9 * self.wear

        return {
            "timestamp": time.time(),
            "step": self.step,
            "vibration": round(vibration, 4),
            "current": round(current, 4),

            "wear_true": round(self.wear, 4),
            "shock_true": shock,
        }


class RawDetector:
    name = "szűretlen"

    def __init__(self, threshold=RAW_THRESHOLD):
        self.threshold = threshold

    def update(self, vibration):
        feature = abs(vibration)
        return feature, feature > self.threshold


class FilteredDetector:
    name = "szűrt"

    def __init__(self, threshold=FILTERED_THRESHOLD, median_window=MEDIAN_WINDOW,
                 rms_window=RMS_WINDOW, confirm=CONFIRM_STEPS, hysteresis=HYSTERESIS):
        self.threshold = threshold
        self.confirm = confirm
        self.hysteresis = hysteresis
        self.raw_buf = deque(maxlen=median_window)
        self.sq_buf = deque(maxlen=rms_window)
        self.sq_sum = 0.0
        self.count = 0
        self.active = False

    def update(self, vibration):
        self.raw_buf.append(vibration)
        filtered = statistics.median(self.raw_buf)

        if len(self.sq_buf) == self.sq_buf.maxlen:
            self.sq_sum -= self.sq_buf[0]
        self.sq_buf.append(filtered * filtered)
        self.sq_sum += filtered * filtered
        if len(self.sq_buf) < self.sq_buf.maxlen:
            return None, False
        rms = math.sqrt(max(self.sq_sum, 0.0) / len(self.sq_buf))

        if not self.active:
            self.count = self.count + 1 if rms > self.threshold else 0
            if self.count >= self.confirm:
                self.active = True
        elif rms < self.threshold * self.hysteresis:
            self.active = False
            self.count = 0
        return rms, self.active


@dataclass
class RunResult:
    false_alarms: int
    healthy_steps: int
    detection_step: int | None
    wear_onset: int
    failure_step: int

    @property
    def delay(self):
        return None if self.detection_step is None else self.detection_step - self.wear_onset

    @property
    def in_time(self):
        return (self.detection_step is not None
                and self.failure_step - self.detection_step >= MIN_LEAD_TIME)

    @property
    def cost(self):
        maintenance = COST_PLANNED if self.in_time else COST_UNPLANNED
        return self.false_alarms * COST_FALSE_ALARM + maintenance


def evaluate_run(detector, sim):
    false_alarms = 0
    detection_step = None
    prev = False
    while sim.step < sim.failure_step:
        d = sim.generate()
        _, active = detector.update(d["vibration"])
        if active and not prev:
            if d["wear_true"] == 0.0:
                false_alarms += 1
            else:
                detection_step = d["step"]
                break
        prev = active
    return RunResult(false_alarms, sim.wear_onset, detection_step, sim.wear_onset, sim.failure_step)


def sweep(detector_cls, thresholds, runs, base_seed=1000):
    rows = []
    for thr in thresholds:
        results = []
        for i in range(runs):
            seed_rng = random.Random(base_seed + i)
            sim = IndustrialSimulator(wear_onset=seed_rng.randint(100, 400), seed=base_seed + i)
            results.append(evaluate_run(detector_cls(threshold=thr), sim))
        delays = [r.delay for r in results if r.delay is not None]
        healthy = sum(r.healthy_steps for r in results)
        rows.append({
            "detector": detector_cls.name,
            "threshold": round(thr, 2),
            "false_alarms_per_1000": 1000 * sum(r.false_alarms for r in results) / healthy,
            "mean_delay": statistics.mean(delays) if delays else float("nan"),
            "missed_rate": sum(not r.in_time for r in results) / runs,
            "mean_cost": statistics.mean(r.cost for r in results),
        })
    return rows


def frange(start, stop, step):
    n = int(round((stop - start) / step)) + 1
    return [start + i * step for i in range(n)]


def run_analyze(args):
    print(f"Küszöb-érzékenységvizsgálat, {args.runs} gépéletciklus / küszöb\n"
          f"Költségmodell: téves riasztás={COST_FALSE_ALARM}, tervezett={COST_PLANNED}, "
          f"nem tervezett={COST_UNPLANNED}, min. átfutási idő={MIN_LEAD_TIME} lépés\n")
    all_rows = (sweep(RawDetector, frange(1.0, 4.0, 0.25), args.runs)
                + sweep(FilteredDetector, frange(0.6, 1.5, 0.1), args.runs))

    header = f"{'detektor':<10} {'küszöb':>6} {'TR/1000':>8} {'késés':>7} {'leállás%':>9} {'ktg/gép':>9}"
    print(header)
    print("-" * len(header))
    last = None
    for r in all_rows:
        if last and r["detector"] != last:
            print()
        last = r["detector"]
        print(f"{r['detector']:<10} {r['threshold']:>6.2f} {r['false_alarms_per_1000']:>8.2f} "
              f"{r['mean_delay']:>7.1f} {100 * r['missed_rate']:>8.1f}% {r['mean_cost']:>9.0f}")

    for name in ("szűretlen", "szűrt"):
        best = min((r for r in all_rows if r["detector"] == name), key=lambda r: r["mean_cost"])
        print(f"\nOptimális küszöb ({name}): {best['threshold']:.2f} -> "
              f"{best['mean_cost']:.0f} / gép, átlagos késés {best['mean_delay']:.1f} lépés")
    print("\nTR/1000 = téves riasztás 1000 ép üzemi lépésenként; késés = kopás kezdetétől az "
          "észlelésig;\nleállás% = nem tervezett leállások aránya (késői vagy elmaradt észlelés).")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=all_rows[0].keys())
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nEredmények mentve: {args.csv}")

    if args.plot:
        plot_tradeoff(all_rows)


def plot_tradeoff(rows):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("A grafikonhoz: pip install matplotlib")
        return
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    for name in ("szűretlen", "szűrt"):
        rs = [r for r in rows if r["detector"] == name]
        ax1.plot([r["false_alarms_per_1000"] for r in rs], [r["mean_delay"] for r in rs],
                 "o-", label=name)
        ax2.plot([r["threshold"] for r in rs], [r["mean_cost"] for r in rs], "o-", label=name)
    ax1.set(xlabel="Téves riasztás / 1000 lépés", ylabel="Átlagos válaszidő [lépés]",
            title="Válaszidő – téves riasztás kompromisszum")
    ax2.set(xlabel="Riasztási küszöb", ylabel="Várható költség / gép",
            title="Küszöb hatása a költségre")
    for ax in (ax1, ax2):
        ax.grid(alpha=0.3)
        ax.legend()
    fig.tight_layout()
    plt.show()


def make_client(client_id):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("A paho-mqtt csomag nem található: pip install paho-mqtt")
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    return mqtt.Client(client_id=client_id)


def connect(client, broker, port):
    try:
        client.connect(broker, port, keepalive=60)
    except OSError as e:
        sys.exit(f"MQTT csatlakozási hiba ({broker}:{port}): {e}\n"
                 f"Fut a helyi broker? (pl. 'mosquitto -v')")


def run_sim(args):
    client = make_client("ipar40-sim")
    connect(client, args.broker, args.port)
    client.loop_start()

    sim = IndustrialSimulator(wear_onset=args.onset, seed=args.seed)
    steps = args.steps or sim.failure_step + 20
    print(f"=== Szimulátor: {args.broker}:{args.port} -> {TOPIC_SENSOR} "
          f"({steps} lépés, kopás kezdete: {sim.wear_onset}. lépés) ===")
    try:
        for _ in range(steps):
            d = sim.generate()
            client.publish(TOPIC_SENSOR, json.dumps(d), qos=1)
            print(f"[{d['step']:4d}] rezgés={d['vibration']:7.3f}  áram={d['current']:6.3f}  "
                  f"kopás={d['wear_true']:.2f}{'  (ütés)' if d['shock_true'] else ''}")
            time.sleep(args.interval)
        client.publish(TOPIC_SENSOR, json.dumps({"end": True}), qos=1).wait_for_publish()
    except KeyboardInterrupt:
        print("\nSzimuláció megszakítva.")
    finally:
        client.loop_stop()
        client.disconnect()


class EdgeNode:
    def __init__(self, client, log_path):
        self.client = client
        self.detectors = [RawDetector(), FilteredDetector()]
        self.prev = {det.name: False for det in self.detectors}
        self.events = {det.name: [] for det in self.detectors}
        self.latencies_ms = []
        self.proc_ms = []
        self.log_file = open(log_path, "w", newline="", encoding="utf-8")
        self.log = csv.writer(self.log_file)
        self.log.writerow(["step", "t_sent", "t_recv", "latency_ms", "proc_ms", "vibration",
                           "current", "wear_true", "raw_feature", "raw_alarm", "rms", "filt_alarm"])
        self.log_path = log_path
        self.last_step = 0
        self.lost = 0
        self.duplicates = 0

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe(TOPIC_SENSOR, qos=1)
        print(f"Feliratkozva: {TOPIC_SENSOR}  (Ctrl+C a leállításhoz)")

    def on_message(self, client, userdata, msg):
        t_recv = time.time()
        t0 = time.perf_counter()
        d = json.loads(msg.payload)
        if d.get("end"):
            self.summary()
            client.disconnect()
            return

        if d["step"] <= self.last_step:
            self.duplicates += 1
            return
        self.lost += d["step"] - self.last_step - 1
        self.last_step = d["step"]

        latency = 1000 * (t_recv - d["timestamp"])
        outputs = {}
        for det in self.detectors:
            feature, active = det.update(d["vibration"])
            outputs[det.name] = (feature, active)
            if active and not self.prev[det.name]:
                self.events[det.name].append((d["step"], d["wear_true"]))
                client.publish(TOPIC_ALARM, json.dumps({
                    "detector": det.name, "step": d["step"], "feature": feature,
                    "threshold": det.threshold, "t_sensor": d["timestamp"], "t_alarm": time.time(),
                }), qos=1)
                print(f"  !!! RIASZTÁS [{det.name}] {d['step']}. lépés, jellemző={feature:.3f}")
            self.prev[det.name] = active

        rms, filt_alarm = outputs["szűrt"]
        if rms is not None:
            client.publish(TOPIC_FILTERED, json.dumps({"step": d["step"], "rms": rms,
                                                       "alarm": filt_alarm}), qos=1)
        proc = 1000 * (time.perf_counter() - t0)
        self.latencies_ms.append(latency)
        self.proc_ms.append(proc)
        raw_f, raw_a = outputs["szűretlen"]
        self.log.writerow([d["step"], d["timestamp"], t_recv, f"{latency:.2f}", f"{proc:.3f}",
                           d["vibration"], d["current"], d["wear_true"], raw_f, int(raw_a),
                           "" if rms is None else f"{rms:.4f}", int(filt_alarm)])
        print(f"[{d['step']:4d}] rezgés={d['vibration']:7.3f}  RMS="
              f"{'  –  ' if rms is None else f'{rms:5.3f}'}  késleltetés={latency:6.1f} ms")

    def summary(self):
        self.log_file.close()
        print("\n=== Összegzés (kiértékelés a ground truth alapján) ===")
        print(f"Üzenetvesztés: {self.lost} db, duplikátum: {self.duplicates} db "
              f"({self.last_step} várt üzenetből)")
        for name, ev in self.events.items():
            false = sum(1 for _, w in ev if w == 0.0)
            true = [s for s, w in ev if w > 0.0]
            first = true[0] if true else "nincs"
            print(f"{name:<10}: {len(ev)} riasztási esemény, ebből téves: {false}, "
                  f"első valódi észlelés: {first}. lépés")
        if self.latencies_ms:
            lat = sorted(self.latencies_ms)
            p95 = lat[int(0.95 * (len(lat) - 1))]
            print(f"Átviteli késleltetés: átlag {statistics.mean(lat):.1f} ms, p95 {p95:.1f} ms, "
                  f"max {lat[-1]:.1f} ms | feldolgozás: átlag {statistics.mean(self.proc_ms):.3f} ms")
        print(f"Napló: {self.log_path}")


def run_edge(args):
    client = make_client("ipar40-edge")
    node = EdgeNode(client, args.log)
    client.on_connect = node.on_connect
    client.on_message = node.on_message
    connect(client, args.broker, args.port)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        node.summary()
        client.disconnect()


class VerificationTests(unittest.TestCase):
    def test_simulator_wear_profile(self):
        sim = IndustrialSimulator(wear_onset=50, wear_duration=100, seed=1)
        data = [sim.generate() for _ in range(160)]
        self.assertTrue(all(d["wear_true"] == 0.0 for d in data[:50]))
        wears = [d["wear_true"] for d in data]
        self.assertEqual(wears, sorted(wears))
        self.assertEqual(data[149]["wear_true"], 1.0)

    def test_simulator_reproducible(self):
        s1, s2 = IndustrialSimulator(seed=7), IndustrialSimulator(seed=7)
        a = [s1.generate()["vibration"] for _ in range(300)]
        b = [s2.generate()["vibration"] for _ in range(300)]
        self.assertEqual(a, b)

    def test_current_rises_with_wear(self):
        sim = IndustrialSimulator(wear_onset=10, wear_duration=100, seed=2)
        data = [sim.generate() for _ in range(110)]
        healthy = statistics.mean(d["current"] for d in data[:10])
        worn = statistics.mean(d["current"] for d in data[-10:])
        self.assertGreater(worn - healthy, 0.7)

    def test_rms_of_constant_signal(self):
        det = FilteredDetector(threshold=10)
        for _ in range(RMS_WINDOW + MEDIAN_WINDOW):
            rms, _ = det.update(1.0)
        self.assertAlmostEqual(rms, 1.0)

    def test_single_shock_raw_alarms_filtered_not(self):
        signal = [0.5] * 20 + [3.5] + [0.5] * 20
        raw, filt = RawDetector(threshold=2.0), FilteredDetector(threshold=0.9)
        self.assertTrue(any(raw.update(x)[1] for x in signal))
        self.assertFalse(any(filt.update(x)[1] for x in signal))

    def test_bounded_response_time(self):
        det = FilteredDetector(threshold=0.9)
        for _ in range(20):
            det.update(0.5)
        for k in range(1, 30):
            if det.update(2.0)[1]:
                break
        self.assertLessEqual(k, MEDIAN_WINDOW // 2 + RMS_WINDOW + CONFIRM_STEPS)

    def test_hysteresis_prevents_chattering(self):
        det = FilteredDetector(threshold=1.0, hysteresis=0.9)
        for _ in range(30):
            det.update(1.2)
        self.assertTrue(det.active)
        for _ in range(30):
            det.update(0.95)
        self.assertTrue(det.active)
        for _ in range(30):
            det.update(0.5)
        self.assertFalse(det.active)

    def test_cost_model(self):
        missed = RunResult(2, 100, None, 100, 300)
        late = RunResult(0, 100, 290, 100, 300)
        ok = RunResult(1, 100, 200, 100, 300)
        self.assertEqual(missed.cost, 2 * COST_FALSE_ALARM + COST_UNPLANNED)
        self.assertEqual(late.cost, COST_UNPLANNED)
        self.assertEqual(ok.cost, COST_FALSE_ALARM + COST_PLANNED)
        self.assertEqual(ok.delay, 100)

    def test_filtered_beats_raw_on_cost(self):
        raw = min(r["mean_cost"] for r in sweep(RawDetector, frange(1.0, 4.0, 0.5), 30))
        filt = min(r["mean_cost"] for r in sweep(FilteredDetector, frange(0.6, 1.4, 0.2), 30))
        self.assertLess(filt, raw)


def main():
    p = argparse.ArgumentParser(description="Prediktív karbantartás – Ipar 4.0 szimuláció")
    sub = p.add_subparsers(dest="mode", required=True)

    for name in ("sim", "edge"):
        sp = sub.add_parser(name)
        sp.add_argument("--broker", default=BROKER)
        sp.add_argument("--port", type=int, default=PORT)
    sub.choices["sim"].add_argument("--interval", type=float, default=0.1, help="mintavételi idő [s]")
    sub.choices["sim"].add_argument("--onset", type=int, default=150, help="kopás kezdete [lépés]")
    sub.choices["sim"].add_argument("--steps", type=int, default=0, help="0 = meghibásodásig + 20")
    sub.choices["sim"].add_argument("--seed", type=int, default=42)
    sub.choices["edge"].add_argument("--log", default="edge_log.csv")

    an = sub.add_parser("analyze")
    an.add_argument("--runs", type=int, default=200)
    an.add_argument("--csv", default="")
    an.add_argument("--plot", action="store_true")

    sub.add_parser("test")

    args = p.parse_args()
    if args.mode == "sim":
        run_sim(args)
    elif args.mode == "edge":
        run_edge(args)
    elif args.mode == "analyze":
        run_analyze(args)
    else:
        unittest.main(argv=[sys.argv[0]], verbosity=2)


if __name__ == "__main__":
    main()
