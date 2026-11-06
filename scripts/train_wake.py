"""
Trains the "Digidaw" detector: a small network on top of openWakeWord's speech embeddings, saved as plain numbers (models/wake/digidaw.npz).
Usage: train_wake.py <examples folder from make_wake_data.py> <room recording .raw, 16 kHz mono s16le> <folder of .wav that is not the name>
       train_wake.py --personal <takes.npz>     the user's own recorded calls on top of the saved examples (name_spotter.py runs this)
"""
import glob
import json
import os
import subprocess
import sys

import numpy as np
from openwakeword.utils import AudioFeatures
from scipy import signal
from sklearn.neural_network import MLPClassifier

RATE = 16000
WINDOW = int(2.24 * RATE)
FRAMES = 16
rng = np.random.default_rng(7)
HIGHPASS = signal.butter(4, 200, "highpass", fs=RATE, output="sos")
HELD_OUT = {"id-ID-GadisNeural", "jv-ID-DimasNeural"}
MINING_ROUNDS = 4
MODEL_DIR = "models/wake"


def load_npz(path):
    data = np.load(path)
    return [(str(v), data[f"c{i}"].astype(np.float32)) for i, v in enumerate(data["voices"])]


def trim(clip):
    energy = np.abs(clip)
    loud = np.flatnonzero(np.convolve(energy, np.ones(160) / 160, "same") > 0.05 * energy.max())
    return clip[loud[0]:loud[-1] + 1] if len(loud) else clip


def room_of(noise):
    start = rng.integers(0, len(noise) - WINDOW)
    return noise[start:start + WINDOW].copy() * rng.uniform(0.6, 1.6)


def echo(clip):
    tail = rng.normal(size=int(RATE * 0.15)) * np.exp(-np.linspace(0, rng.uniform(4, 9), int(RATE * 0.15)))
    tail[0] = 3
    wet = signal.fftconvolve(clip, tail)[:len(clip)]
    return wet / (np.abs(wet).max() + 1e-6) * np.abs(clip).max()


def place(clip, noise, end_gap):
    """The clip in the room, ending end_gap seconds before the window ends."""
    window = room_of(noise)
    if rng.random() < 0.5:
        clip = echo(clip)
    level = rng.uniform(1000, 9000) / (np.sqrt(np.mean(clip ** 2)) + 1e-6)
    clip = clip * level
    end = WINDOW - int(end_gap * RATE)
    start = max(0, end - len(clip))
    window[start:end] += clip[-(end - start):]
    return window


def embed(windows, features):
    x = np.clip(signal.sosfilt(HIGHPASS, np.array(windows), axis=1), -32768, 32767).astype(np.int16)
    return features.embed_clips(x, batch_size=64, ncpu=4)[:, -FRAMES:, :].reshape(len(x), -1)


def stream_scores(audio, features, net):
    """A score every 80 ms, as the running detector would give."""
    filtered = np.clip(signal.sosfilt(HIGHPASS, audio), -32768, 32767).astype(np.int16)
    all_frames = []
    for begin in range(0, len(filtered) - WINDOW, RATE * 30):
        all_frames.append(features.embed_clips(filtered[begin:begin + RATE * 30 + WINDOW][None], batch_size=1, ncpu=4)[0])
    frames = np.concatenate(all_frames)
    grid = np.lib.stride_tricks.sliding_window_view(frames, FRAMES, axis=0).transpose(0, 2, 1).reshape(-1, FRAMES * 96)
    return net.predict_proba(grid)[:, 1]


def stream_windows(audio, features, net, above):
    """The windows of a long sound that score above this."""
    filtered = np.clip(signal.sosfilt(HIGHPASS, audio), -32768, 32767).astype(np.int16)
    found = []
    for begin in range(0, len(filtered) - WINDOW, RATE * 30):
        frames = features.embed_clips(filtered[begin:begin + RATE * 30 + WINDOW][None], batch_size=1, ncpu=4)[0]
        grid = np.lib.stride_tricks.sliding_window_view(frames, FRAMES, axis=0).transpose(0, 2, 1).reshape(-1, FRAMES * 96)
        found.append(grid[net.predict_proba(grid)[:, 1] >= above])
    return np.vstack(found) if found else np.empty((0, FRAMES * 96))


def read_wav(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32)


def false_fires(scores, threshold):
    """How many separate moments cross the threshold: one word gives a run of scores."""
    above = scores >= threshold
    above = above[1:] & above[:-1]                       # two in a row (160 ms): the running detector asks the same
    return int(np.sum(above[1:] & ~above[:-1]) + (above[0] if len(above) else 0))


def main(folder, room_file, others, *calls):
    features = AudioFeatures(inference_framework="onnx", ncpu=4)
    raw = np.fromfile(room_file, dtype=np.int16).astype(np.float32)
    name = [(v, trim(c)) for v, c in load_npz(f"{folder}/name.npz") if len(trim(c)) <= 1.5 * RATE]
    close = load_npz(f"{folder}/close.npz")
    talk = load_npz(f"{folder}/talk.npz")
    waves = {os.path.basename(p): read_wav(p) for p in sorted(glob.glob(f"{others}/*.wav"))}
    print("nama", len(name), "mirip", len(close), "kalimat", len(talk), "rekaman", {k[:14]: len(v) // RATE for k, v in waves.items()}, flush=True)
    room_train, room_test = raw[:int(24 * RATE)], raw[int(34 * RATE):]
    room_all = np.concatenate([room_train, room_test])
    pos, pos_test = [], []
    for voice, clip in name:
        target = pos_test if voice in HELD_OUT else pos
        for _ in range(3):
            target.append(place(clip, room_all, rng.uniform(0.0, 0.3)))
    neg = []
    for voice, clip in close:
        for _ in range(2):
            neg.append(place(trim(clip), room_all, rng.uniform(0.0, 0.5)))
    for voice, clip in talk:
        for _ in range(6):
            start = rng.integers(0, max(1, len(clip) - WINDOW // 2))
            neg.append(place(clip[start:start + WINDOW // 2], room_all, rng.uniform(0.0, 0.6)))
    for _ in range(1500):
        neg.append(room_of(room_all))
    for key, wave in waves.items():
        usable = wave[:len(wave) // 2]                                    # the other half stays for the test
        for _ in range(350):
            start = rng.integers(0, len(usable) - WINDOW)
            mixed = usable[start:start + WINDOW] * rng.uniform(0.03, 0.5) + room_of(room_all)
            neg.append(mixed)
    print("contoh latih: positif", len(pos), "negatif", len(neg), flush=True)
    x_pos, x_neg = embed(pos, features), embed(neg, features)
    # the mistakes teach most: what it wrongly takes for the name is added as "not the name", a few rounds, from the half kept for training
    streams = [wave[:len(wave) // 2] * gain + np.resize(room_all, len(wave) // 2) for wave in waves.values() for gain in (0.25, 0.08)]
    streams += [np.concatenate([clip for _, clip in talk]) * 0.3 + 0]
    net = None
    for round_number in range(MINING_ROUNDS):
        net = MLPClassifier(hidden_layer_sizes=(128, 32), alpha=1e-3, batch_size=256, max_iter=60, early_stopping=True, random_state=7)
        net.fit(np.vstack([x_pos, x_neg]), np.concatenate([np.ones(len(x_pos)), np.zeros(len(x_neg))]))
        hard = []
        for stream in streams:
            hard.append(stream_windows(stream, features, net, 0.1))
        hard = np.vstack(hard) if hard else np.empty((0, FRAMES * 96))
        print(f"putaran {round_number + 1}: contoh sulit {len(hard)}", flush=True)
        if not len(hard):
            break
        x_neg = np.vstack([x_neg, hard])
    test = net.predict_proba(embed(pos_test, features))[:, 1]
    print("suara yang tidak ikut dilatih:", {t: f"{np.mean(test >= t) * 100:.0f}%" for t in (0.5, 0.7, 0.9, 0.95, 0.99)}, flush=True)
    for key, wave in waves.items():
        half = wave[len(wave) // 2:] * 0.25 + np.resize(room_all, len(wave) - len(wave) // 2)
        scores = stream_scores(half, features, net)
        minutes = len(half) / RATE / 60
        print(f"  {key[:30]:30s} {minutes:.1f} mnt: palsu", {t: false_fires(scores, t) for t in (0.5, 0.7, 0.9, 0.95, 0.99)}, flush=True)
    scores = stream_scores(room_test, features, net)
    print(f"  ruangan {len(room_test) / RATE:.0f} dtk: palsu", {t: false_fires(scores, t) for t in (0.5, 0.7, 0.9, 0.95, 0.99)}, "tertinggi", round(float(scores.max()), 3), flush=True)
    around = stream_scores(raw[int(24 * RATE):int(34 * RATE)], features, net)
    print("rekaman detik 24-34:", [(round(24 + 2.24 + i * 0.08, 1), round(float(v), 2)) for i, v in enumerate(around) if v >= 0.3], flush=True)
    os.makedirs(MODEL_DIR, exist_ok=True)
    np.savez_compressed(f"{MODEL_DIR}/train_set.npz", pos=x_pos.astype(np.float16), neg=x_neg.astype(np.float16))
    save(net, f"{MODEL_DIR}/digidaw.npz", 0.9)
    print("tersimpan", os.path.getsize(f"{MODEL_DIR}/digidaw.npz") // 1024, "KB; contoh", os.path.getsize(f"{MODEL_DIR}/train_set.npz") // 1024 // 1024, "MB")


def save(net, path, threshold):
    part = path + ".part.npz"
    np.savez(part, threshold=np.array(threshold), **{f"w{i}": w for i, w in enumerate(net.coefs_)}, **{f"b{i}": b for i, b in enumerate(net.intercepts_)})
    os.replace(part, path)


def _at_own_level(clip, noise, end_gap):
    """A recorded take in the room as it is, a little louder or softer: this is how this microphone hears this user."""
    window = room_of(noise)
    clip = clip * rng.uniform(0.6, 1.5)
    end = WINDOW - int(end_gap * RATE)
    start = max(0, end - len(clip))
    window[start:end] += clip[-(end - start):]
    return window


def personal(takes_file):
    """Learn the user's voice. Every fourth call is kept out to see how well it works on takes it hasn't seen."""
    takes = np.load(takes_file)
    room = takes["room"].astype(np.float32)
    calls = [trim(takes[k].astype(np.float32)) for k in sorted(k for k in takes.files if k.startswith("call"))]
    others = [takes[k].astype(np.float32) for k in sorted(k for k in takes.files if k.startswith("other"))]
    if len(room) < WINDOW:
        room = np.resize(room, WINDOW * 2)
    features = AudioFeatures(inference_framework="onnx", ncpu=2)
    base = np.load(f"{MODEL_DIR}/train_set.npz")
    held = calls[::4]
    learn = [c for i, c in enumerate(calls) if i % 4]

    def fit(clips):
        pos = [_at_own_level(c, room, rng.uniform(0.0, 0.3)) for c in clips for _ in range(60)]
        neg = [_at_own_level(c, room, rng.uniform(0.0, 0.5)) for c in others for _ in range(40)] + [room_of(room) for _ in range(400)]
        x = np.vstack([base["pos"].astype(np.float32), embed(pos, features), base["neg"].astype(np.float32), embed(neg, features)])
        y = np.concatenate([np.ones(len(base["pos"]) + len(pos)), np.zeros(len(base["neg"]) + len(neg))])
        net = MLPClassifier(hidden_layer_sizes=(128, 32), alpha=1e-3, batch_size=256, max_iter=60, early_stopping=True, random_state=7)
        return net.fit(x, y)

    net = fit(learn)
    seen = [float(net.predict_proba(embed([_at_own_level(c, room, gap) for gap in (0.05, 0.15, 0.25)], features))[:, 1].max()) for c in held]
    wrong = [float(net.predict_proba(embed([_at_own_level(c, room, gap) for gap in (0.05, 0.25)], features))[:, 1].max()) for c in others]
    threshold = float(np.clip(min(seen) * 0.9, 0.5, 0.9)) if seen else 0.9
    net = fit(calls)
    save(net, f"{MODEL_DIR}/digidaw_personal.npz", threshold)
    print(json.dumps({"held": sum(s >= threshold for s in seen), "of": len(seen), "wrong": sum(w >= threshold for w in wrong), "others": len(wrong), "threshold": round(threshold, 2)}))


if __name__ == "__main__":
    if sys.argv[1] == "--personal":
        personal(sys.argv[2])
    else:
        main(*sys.argv[1:])
