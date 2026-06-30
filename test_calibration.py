"""M4 calibration check — 4 deliberately chosen inputs, both signals shown
separately so a misbehaving signal is visible. Run: python3 test_calibration.py
"""

from main import (
    attribution_from_p_ai,
    combine_signals,
    signal_heuristics,
    signal_llm,
)

CASES = [
    ("clearly AI", "high", (
        "Artificial intelligence represents a transformative paradigm shift in "
        "modern society. It is important to note that while the benefits of AI "
        "are numerous, it is equally essential to consider the ethical "
        "implications. Furthermore, stakeholders across various sectors must "
        "collaborate to ensure responsible deployment."
    )),
    ("clearly human", "low", (
        "ok so i finally tried that new ramen place downtown and honestly? "
        "underwhelming. the broth was fine but they put WAY too much sodium in "
        "it and i was thirsty for like three hours after. my friend got the "
        "spicy version and said it was better. probably won't go back unless "
        "someone drags me there"
    )),
    ("borderline: formal human", "mid-high", (
        "The relationship between monetary policy and asset price inflation has "
        "been extensively studied in the literature. Central banks face a "
        "fundamental tension between their mandate for price stability and the "
        "unintended consequences of prolonged low interest rates on equity and "
        "real estate valuations."
    )),
    ("borderline: lightly edited AI", "mid", (
        "I've been thinking a lot about remote work lately. There are genuine "
        "tradeoffs — flexibility and no commute on one side, isolation and "
        "blurred work-life boundaries on the other. Studies show productivity "
        "varies widely by individual and role type."
    )),
]


def fmt(x):
    return "None" if x is None else f"{x:.2f}"


def main():
    header = (f"{'case':<32} {'expect':>9} {'sig1':>6} {'sig2':>6} "
              f"{'combined':>9} {'attribution':>14}")
    print(header)
    print("-" * len(header))
    for label, expect, text in CASES:
        s1 = signal_llm(text)
        s2 = signal_heuristics(text)
        combined, degraded = combine_signals(s1, s2)
        attribution = attribution_from_p_ai(combined)
        flag = " (deg)" if degraded else ""
        print(f"{label:<32} {expect:>9} {fmt(s1):>6} {fmt(s2):>6} "
              f"{fmt(combined):>9} {attribution:>14}{flag}")


if __name__ == "__main__":
    main()
