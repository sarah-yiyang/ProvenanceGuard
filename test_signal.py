"""Independent test of both detection signals — calls the functions directly,
before they're trusted through the /submit endpoint. Run: python3 test_signal.py

signal_llm     = Signal 1 (Groq LLM classifier, needs GROQ_API_KEY)
signal_heuristics = Signal 2 (local stylometry, no network)
"""

from main import (
    attribution_from_p_ai,
    combine_signals,
    signal_heuristics,
    signal_llm,
)

CASES = [
    ("clearly AI", (
        "Artificial intelligence has revolutionized numerous industries by "
        "enabling unprecedented levels of efficiency and innovation. From "
        "healthcare to finance, organizations are leveraging these powerful "
        "tools to streamline operations and deliver enhanced value to "
        "stakeholders across the board."
    )),
    ("clearly human", (
        "ok so i FINALLY fixed the dishwasher lol. turned out the kid had "
        "jammed a lego brick down the drain thing?? took me like an hour of "
        "swearing under the sink. anyway it works now, mostly."
    )),
    ("formulaic human (edge case)", (
        "Preheat oven to 350F. Mix flour, sugar, and salt. Add butter. Stir "
        "until combined. Pour into pan. Bake for 25 minutes. Let cool. Serve."
    )),
]


def fmt(x):
    return "None" if x is None else f"{x:.2f}"


def main():
    header = f"{'case':<30} {'sig1':>6} {'sig2':>6} {'combined':>9} {'attribution':>15}"
    print(header)
    print("-" * len(header))
    for label, text in CASES:
        s1 = signal_llm(text)
        s2 = signal_heuristics(text)
        combined, degraded = combine_signals(s1, s2)
        attribution = attribution_from_p_ai(combined)
        flag = " (degraded)" if degraded else ""
        print(f"{label:<30} {fmt(s1):>6} {fmt(s2):>6} {fmt(combined):>9} "
              f"{attribution:>15}{flag}")


if __name__ == "__main__":
    main()
