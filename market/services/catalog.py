"""Static reference data for NSE listings: official sector classification and news aliases."""

SECTORS: dict[str, list[str]] = {
    "Agricultural": ["AMAC", "EGAD", "KAPC", "KUKZ", "LIMT", "SASN", "WTK"],
    "Automobiles & Accessories": ["CGEN"],
    "Banking": ["ABSA", "BKG", "COOP", "DTK", "EQTY", "FMLY", "HFCB", "IMH", "KCB", "NCBA", "SBIC", "SCBK"],
    "Commercial & Services": [
        "DCON", "EVRD", "HBE", "KQ", "LKL", "NBV", "NMG", "QMRT", "SCAN", "SGL", "SMER",
        "TPSE", "UCHM", "XPRS",
    ],
    "Construction & Allied": ["ARM", "BAMB", "CABL", "CRWN", "PORT"],
    "Energy & Petroleum": ["KEGN", "KPC", "KPLC", "KPLC-P4", "KPLC-P7", "TOTL", "UMME"],
    "Insurance": ["BRIT", "CIC", "JUB", "KNRE", "LBTY", "SLAM"],
    "Investment": ["CTUM", "HAFR", "KURV", "OCH", "TCL"],
    "Investment Services": ["NSE"],
    "Manufacturing & Allied": ["BAT", "BOC", "CARB", "EABL", "FTGH", "MSC", "SKL", "UNGA"],
    "Telecommunication": ["SCOM"],
    "Real Estate Investment Trust": ["ALP", "LAPR", "TRFC"],
    "Exchange Traded Fund": ["GLD", "SMWF"],
}

TICKER_SECTOR = {t: s for s, ts in SECTORS.items() for t in ts}

# Instruments that are not ordinary shares: excluded from stock picks.
NON_EQUITY = {"GLD", "SMWF", "KPLC-P4", "KPLC-P7"}

# ticker -> (search phrase, *extra headline match terms)
NEWS_ALIASES: dict[str, tuple[str, ...]] = {
    "ABSA": ("Absa Bank Kenya", "Absa Kenya"),
    "ALP": ("ALP REIT", "Africa Logistics Properties"),
    "AMAC": ("Africa Mega Agricorp",),
    "ARM": ("ARM Cement",),
    "BAMB": ("Bamburi Cement", "Bamburi"),
    "BAT": ("BAT Kenya", "British American Tobacco Kenya"),
    "BKG": ("BK Group", "Bank of Kigali"),
    "BOC": ("BOC Kenya",),
    "BRIT": ("Britam",),
    "CABL": ("East African Cables",),
    "CARB": ("Carbacid",),
    "CGEN": ("Car & General", "Car and General"),
    "CIC": ("CIC Insurance", "CIC Group"),
    "COOP": ("Co-operative Bank of Kenya", "Co-op Bank", "Co-operative Bank", "Cooperative Bank"),
    "CRWN": ("Crown Paints",),
    "CTUM": ("Centum",),
    "DCON": ("Deacons East Africa", "Deacons"),
    "DTK": ("Diamond Trust Bank", "DTB"),
    "EABL": ("EABL", "East African Breweries"),
    "EGAD": ("Eaagads",),
    "EQTY": ("Equity Group", "Equity Bank"),
    "EVRD": ("Eveready East Africa", "Eveready"),
    "FMLY": ("Family Bank",),
    "FTGH": ("Flame Tree Group",),
    "GLD": ("Absa NewGold ETF", "NewGold"),
    "HAFR": ("Home Afrika",),
    "HBE": ("Homeboyz Entertainment",),
    "HFCB": ("HF Group", "HFC Kenya", "HFCK"),
    "IMH": ("I&M Group", "I&M Bank", "I&M Holdings"),
    "JUB": ("Jubilee Holdings", "Jubilee Insurance"),
    "KAPC": ("Kapchorua Tea",),
    "KCB": ("KCB Group", "KCB Bank"),
    "KEGN": ("KenGen",),
    "KNRE": ("Kenya Re", "Kenya Reinsurance"),
    "KPC": ("Kenya Pipeline",),
    "KPLC": ("Kenya Power", "KPLC"),
    "KPLC-P4": ("Kenya Power",),
    "KPLC-P7": ("Kenya Power",),
    "KQ": ("Kenya Airways",),
    "KUKZ": ("Kakuzi",),
    "KURV": ("Kurwitu Ventures",),
    "LAPR": ("Laptrust Imara", "Imara I-REIT"),
    "LBTY": ("Liberty Kenya",),
    "LIMT": ("Limuru Tea",),
    "LKL": ("Longhorn Publishers",),
    "MSC": ("Mumias Sugar",),
    "NBV": ("Nairobi Business Ventures",),
    "NCBA": ("NCBA",),
    "NMG": ("Nation Media Group", "NMG"),
    "NSE": ("Nairobi Securities Exchange", "NSE Kenya"),
    "OCH": ("Olympia Capital",),
    "PORT": ("East African Portland Cement", "EAPCC", "EA Portland"),
    "QMRT": ("Quickmart", "Quick Mart"),
    "SASN": ("Sasini",),
    "SBIC": ("Stanbic Holdings", "Stanbic Kenya", "Stanbic Bank Kenya"),
    "SCAN": ("WPP Scangroup", "Scangroup"),
    "SCBK": ("Standard Chartered Kenya", "StanChart Kenya", "Standard Chartered Bank Kenya"),
    "SCOM": ("Safaricom", "M-Pesa"),
    "SGL": ("Standard Group",),
    "SKL": ("Shri Krishana Overseas",),
    "SLAM": ("Sanlam Kenya", "Sanlam Allianz"),
    "SMER": ("Sameer Africa",),
    "SMWF": ("Satrix MSCI World",),
    "TCL": ("TransCentury", "Trans-Century"),
    "TOTL": ("TotalEnergies Kenya", "Total Kenya"),
    "TPSE": ("TPS Serena", "Serena Hotels"),
    "TRFC": ("Trific", "TRIFIC REIT"),
    "UCHM": ("Uchumi",),
    "UMME": ("Umeme",),
    "UNGA": ("Unga Group",),
    "WTK": ("Williamson Tea",),
    "XPRS": ("Express Kenya",),
}


# Extra words added to the news *search* (not the headline filter) to keep results on-topic.
# Default is "Kenya"; counters domiciled elsewhere use their home market.
NEWS_CONTEXT: dict[str, str] = {
    "BKG": "Rwanda",
    "UMME": "Uganda",
    "SCOM": "",
    "KQ": "",
    "EABL": "",
    "KCB": "",
    "NCBA": "",
}


def news_context(ticker: str) -> str:
    return NEWS_CONTEXT.get(ticker, "Kenya")


def sector_for(ticker: str) -> str:
    return TICKER_SECTOR.get(ticker, "Other")


def news_terms(ticker: str, name: str) -> tuple[str, list[str]]:
    """Return (search phrase, headline match terms) for a ticker."""
    aliases = NEWS_ALIASES.get(ticker)
    if not aliases:
        base = name
        for suffix in (" Plc", " Ltd", " Limited", " Holdings", " Group", " Company"):
            base = base.replace(suffix, "")
        aliases = (base.strip(),)
    return aliases[0], [a.lower() for a in aliases]
