"""Plain-language notes that interpret the PCA, eigenvalue and Markov-chain experiments.

Everything here is generated from the live results, so the wording tracks the data.
"""

from collections import Counter

COSTS_EACH_WAY = 0.021


def _p(x: float, nd: int = 0) -> str:
    return f"{x * 100:.{nd}f}%"


def _sector_of(items: list[dict]) -> str | None:
    c = Counter(i["sector"] for i in items).most_common(1)
    return c[0][0] if c and c[0][1] >= 2 else None


def pca_notes(p: dict) -> dict:
    s1, n, bets, k80 = p["explained"][0], p["n_stocks"], p["effective_bets"], p["k80"]
    if s1 < 0.2:
        strength = "weak"
        impl = "Company-specific news drives most price moves, so spreading money across several counters cuts risk a lot."
    elif s1 < 0.4:
        strength = "moderate"
        impl = "Part of every move is market-wide. Diversifying still helps, but it can't remove the shared part."
    else:
        strength = "strong"
        impl = "Stocks largely move as a pack. Adding more NSE counters gives little extra protection; bonds or T-bills diversify better."
    c1 = p["components"][0]
    findings = [
        f"The market factor (PC1) explains {_p(s1, 1)} of daily price moves across {n} liquid counters "
        f"({p['start']} to {p['end']}). That is a {strength} market factor.",
        f"Owning all {n} counters behaves like holding about {bets:.0f} independent bets. "
        f"It takes {k80} of the {n} components to explain 80% of the movement.",
        f"Most tied to the market: {', '.join(c1['top'])}. Least tied: {', '.join(c1['bottom'])}.",
    ]
    meaning = [impl, f"Counters with low PC1 loadings, such as {', '.join(c1['bottom'][:2])}, are the best diversifiers to add."]
    if len(p["components"]) > 1:
        c2 = p["components"][1]
        hi, lo = c2["loadings"][:5], c2["loadings"][-5:]
        sa, sb = _sector_of(hi), _sector_of(lo)
        theme = f" ({sa} versus {sb})" if sa and sb and sa != sb else ""
        findings.append(
            f"The second component ({_p(c2['explained'], 1)}) pulls {', '.join(c2['top'])} one way and "
            f"{', '.join(c2['bottom'])} the other{theme}: a theme that cuts across the market."
        )
        meaning.append("Holding counters from both ends of PC2 spreads your exposure to that second theme.")
    return {
        "key": "pca",
        "title": "Principal component analysis",
        "measures": "PCA finds the hidden factors that move many stocks together. The first factor is almost always "
        "the market itself; later ones are themes such as sectors.",
        "findings": findings,
        "meaning": meaning,
        "caveats": [
            "Thinly traded counters look less correlated than they really are, because their prices don't update every day.",
            "This uses the last year only; the factor structure shifts over time.",
        ],
    }


def eigen_notes(e: dict) -> dict:
    m, lp, l1, n = e["signal"], e["lambda_plus"], e["market_mode"], e["n_stocks"]
    findings = [
        f"{m} of {n} eigenvalues {'rises' if m == 1 else 'rise'} above the random-noise bound (λ+ = {lp:.2f}). "
        f"The largest is {l1:.2f}, {l1 / lp:.1f}× the bound.",
        "Apart from the market factor, the remaining correlations look like random noise over this window."
        if m <= 1 else
        f"Besides the market factor there {'is' if m == 2 else 'are'} {m - 1} more real factor{'s' if m > 2 else ''}, "
        "most likely sector or size groupings.",
    ]
    meaning = []
    pct, ar = e.get("ar_percentile"), e.get("ar_now")
    if ar is not None and pct is not None:
        chg = e.get("ar_change_3m")
        pts = f"{abs(chg) * 100:.1f} points" if chg is not None else ""
        trend = "" if chg is None else (f", up {pts} in three months" if chg > 0.005 else
                                        f", down {pts} in three months" if chg < -0.005 else ", flat over three months")
        findings.append(f"The absorption ratio is {_p(ar, 1)}, higher than {_p(pct)} of readings in the last two years{trend}.")
        if pct >= 0.8:
            meaning.append("Stocks are unusually tightly coupled. High and rising absorption has historically come before bigger "
                           "market-wide swings, and diversification protects less right now. Keep position sizes moderate and "
                           "hold some cash or T-bills.")
        elif pct <= 0.2:
            meaning.append("Stocks are loosely coupled, so picking the right counters matters more than the market's direction.")
        else:
            meaning.append("Coupling is in its normal range; no special caution is signalled.")
    meaning.append("Correlations below the noise bound are unreliable. Don't build pairs trades or hedges on them.")
    return {
        "key": "eigen",
        "title": "Eigenvalues",
        "measures": "Each eigenvalue of the correlation matrix is the strength of one factor. Random-matrix theory "
        "(Marchenko–Pastur) says how big eigenvalues would be from pure chance, so anything above that bound is real.",
        "findings": findings,
        "meaning": meaning,
        "caveats": [
            "The noise bound assumes independent, normally distributed returns; fat tails and stale prices blur it.",
            "The absorption ratio is a warning light, not a timing tool.",
        ],
    }


def markov_notes(mk: dict) -> dict:
    t, pval, base, uau = mk["ticker"], mk["p_value"], mk["up_base"], mk["up_after_up"]
    pi = mk["stationary"]
    findings = [
        (f"Yesterday's move does help predict today's for {t} (χ² test, p = {pval:.1g})."
         if mk["has_memory"] else
         f"There is no reliable link between one day and the next for {t} (χ² test, p = {pval:.2f})."),
    ]
    if uau > base + 0.05:
        pattern = "short-term momentum"
    elif uau < base - 0.05:
        pattern = "mean reversion"
    else:
        pattern = "little pattern"
    findings.append(f"After an up day, the next day is up {_p(uau)} of the time versus {_p(base)} on average. That is {pattern}.")
    if mk.get("half_life"):
        findings.append(f"The memory fades fast. Its half-life is {mk['half_life']:.1f} trading days, "
                        "so within a week the odds are back to normal.")
    findings.append(f"Latest session: {mk['current']}. Chance the next day is up: {_p(mk['p_up_1'])} (normally {_p(base)}).")
    findings.append(f"Over {mk['n_days']} days, {t} rose on {_p(pi[3] + pi[4])} of days, fell on {_p(pi[0] + pi[1])} "
                    f"and was flat on {_p(pi[2])}.")
    edge = abs(mk["p_up_1"] - base)
    meaning = [
        f"The edge ({_p(edge)} in the odds of an up day) is small next to trading costs of about "
        f"{_p(COSTS_EACH_WAY, 1)} each way, so this is not a trading signal on its own.",
        "Use it to time purchases a little better (for example, not buying straight after a big up day), not to trade every day.",
    ]
    if t == "NASI" and uau > base + 0.05:
        meaning.insert(0, "Index momentum often comes from thinly traded shares catching up a day late, not from "
                          "predictable investor behaviour.")
    return {
        "key": "markov",
        "title": f"Markov chain · {t}",
        "measures": "A Markov chain sorts each day into a state (big down, down, flat, up, big up) and counts how often "
        "each state follows another. That shows whether today's move says anything about tomorrow's.",
        "findings": findings,
        "meaning": meaning,
        "caveats": [
            "It assumes the odds stayed stable over three years. A change of regime (interest rates, elections, policy) can break it.",
            "Pick any counter in the Markov chain tab; these notes describe the index unless it has no history yet.",
        ],
    }


def build(pca: dict | None, eig: dict | None, mk: dict | None, errors: dict, acc: dict | None = None) -> dict:
    sections = []
    if pca:
        sections.append(pca_notes(pca))
    if eig:
        sections.append(eigen_notes(eig))
    if mk:
        sections.append(markov_notes(mk))
    if acc:
        sections.append(acc)

    bottom = []
    if pca:
        bottom.append("Diversify across counters with low market loadings." if pca["explained"][0] < 0.4
                      else "Look beyond NSE equities for diversification; local stocks move together.")
    if eig and eig.get("ar_percentile") is not None:
        bottom.append("Size positions cautiously while coupling is high." if eig["ar_percentile"] >= 0.8
                      else "Market coupling gives no special warning.")
    if mk:
        bottom.append("Don't chase day-to-day patterns; they are smaller than trading costs.")
    if acc and acc.get("bottom"):
        bottom.append(acc["bottom"])
    return {
        "sections": sections,
        "errors": errors,
        "together": [
            "PCA and the eigenvalue spectrum compare many stocks on the same day. The Markov chain follows one series through time.",
            "All three rest on eigenvalues. In PCA they measure how much movement each hidden factor carries. In the Markov "
            "chain, the eigenvalue 1 gives the long-run odds, and the second-largest eigenvalue sets how fast the memory fades.",
        ],
        "bottom_line": bottom,
        "glossary": [
            ("Eigenvector / eigenvalue", "A direction in the data and how much variation lies along it."),
            ("Loading", "How strongly one stock moves with a factor (−1 to 1)."),
            ("Independent bets", "How many truly separate risks a portfolio of all the counters holds."),
            ("Noise bound λ+", "The largest eigenvalue pure randomness would produce for this many stocks and days."),
            ("Absorption ratio", "Share of all movement captured by the top factors; higher means stocks move more together."),
            ("Transition matrix", "The odds of moving from today's state to each possible state tomorrow."),
            ("Stationary distribution", "The long-run share of days spent in each state."),
            ("p-value", "Below 0.05 means the pattern is unlikely to be chance."),
        ],
    }
