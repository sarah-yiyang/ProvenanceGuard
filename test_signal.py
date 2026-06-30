"""Independent test of Signal 1 (signal_llm) — calls the function directly,
before it's trusted through the /submit endpoint. Run: python3 test_signal.py
"""

from main import signal_llm

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


def main():
    for label, text in CASES:
        p_ai = signal_llm(text)
        verdict = "ABSTAINED (None)" if p_ai is None else f"p_ai = {p_ai:.2f}"
        print(f"[{label}]\n  {verdict}\n")


if __name__ == "__main__":
    main()
