"""Offline vocabularies: drug / route / unit / procedure synonyms (English, Hinglish, Devanagari).

RxNorm ingredient RXCUIs are included for common pre-hospital drugs. They are for the demo
only; verify against an RxNorm release before any production use.
Typical max single adult doses are a *sanity check* to catch ASR/LLM errors
(e.g. "3 mg" epinephrine heard instead of "0.3 mg"), not clinical guidance.
"""

# canonical name -> (rxcui, [synonyms], typical max single adult dose as (value, unit) or None)
DRUGS: dict[str, tuple[str | None, list[str], tuple[float, str] | None]] = {
    "epinephrine": ("3992", ["epi", "adrenaline", "adrenalin", "epinephrin", "एपिनेफ्रिन", "एड्रेनालिन", "एड्रेनालाईन"], (1.0, "mg")),
    "naloxone": ("7242", ["narcan", "nalox", "नालोक्सोन"], (4.0, "mg")),
    "aspirin": ("1191", ["asa", "disprin", "ecosprin", "एस्पिरिन", "डिस्प्रिन"], (600.0, "mg")),
    "nitroglycerin": ("4917", ["ntg", "nitro", "glyceryl trinitrate", "gtn", "nitroglycerine", "नाइट्रोग्लिसरीन"], (0.8, "mg")),
    "isosorbide dinitrate": ("6058", ["sorbitrate", "isordil", "isdn"], (10.0, "mg")),
    "albuterol": ("435", ["salbutamol", "asthalin", "ventolin", "सालबुटामोल"], (5.0, "mg")),
    "ipratropium": ("7213", ["atrovent", "ipravent", "duolin"], (0.5, "mg")),
    "morphine": ("7052", ["mso4", "मॉर्फिन"], (10.0, "mg")),
    "fentanyl": ("4337", ["fenta", "फेंटानिल"], (200.0, "mcg")),
    "ketamine": ("6130", ["ketalar", "केटामाइन"], (200.0, "mg")),
    "ondansetron": ("26225", ["zofran", "ondem", "emset", "ओंडांसेट्रॉन"], (8.0, "mg")),
    "metoclopramide": ("6915", ["perinorm", "reglan"], (20.0, "mg")),
    "glucose": ("4850", ["dextrose", "d50", "d25", "d10", "ग्लूकोज", "डेक्सट्रोज"], (50.0, "g")),
    "amiodarone": ("703", ["cordarone", "एमियोडेरोन"], (300.0, "mg")),
    "adenosine": ("296", ["adenocard", "एडेनोसिन"], (12.0, "mg")),
    "atropine": ("1223", ["एट्रोपिन"], (1.0, "mg")),
    "midazolam": ("6960", ["versed", "mezolam", "मिडाज़ोलम"], (10.0, "mg")),
    "diazepam": ("3322", ["valium", "calmpose", "डायजेपाम"], (10.0, "mg")),
    "lorazepam": ("6470", ["ativan", "लोराज़ेपाम"], (4.0, "mg")),
    "diphenhydramine": ("3498", ["benadryl"], (50.0, "mg")),
    "pheniramine": (None, ["avil"], (45.5, "mg")),
    "hydrocortisone": ("5492", ["solu-cortef", "efcorlin", "हाइड्रोकोर्टिसोन"], (200.0, "mg")),
    "dexamethasone": ("3264", ["dexona", "decadron"], (16.0, "mg")),
    "methylprednisolone": ("6902", ["solu-medrol", "medrol"], (125.0, "mg")),
    "acetaminophen": ("161", ["paracetamol", "pcm", "crocin", "dolo", "calpol", "tylenol", "पैरासिटामोल"], (1000.0, "mg")),
    "ibuprofen": ("5640", ["brufen", "advil", "आइबुप्रोफेन"], (800.0, "mg")),
    "tranexamic acid": ("10691", ["txa", "tranexamic", "pause"], (2.0, "g")),
    "magnesium sulfate": ("6585", ["mgso4", "magnesium"], (4.0, "g")),
    "furosemide": ("4603", ["lasix", "frusemide", "लेसिक्स"], (80.0, "mg")),
    "clopidogrel": ("32968", ["plavix", "clopilet"], (600.0, "mg")),
    "heparin": ("5224", ["हेपरिन"], None),
    "sodium chloride 0.9%": ("9863", ["normal saline", "ns", "saline", "nacl", "सलाइन", "नॉर्मल सलाइन"], None),
    "ringer lactate": (None, ["rl", "ringer's lactate", "lactated ringer", "lr", "रिंगर लैक्टेट"], None),
    "oxygen": ("7806", ["o2", "ऑक्सीजन"], None),
    "activated charcoal": (None, ["charcoal"], None),
}

ROUTES: dict[str, list[str]] = {
    "IV": ["iv", "intravenous", "intravenously", "vein", "nas mein", "नस में", "नस से", "drip"],
    "IM": ["im", "intramuscular", "intramuscularly", "muscle", "thigh", "deltoid", "मांसपेशी"],
    "IO": ["io", "intraosseous"],
    "SC": ["sc", "subcut", "subcutaneous", "sq", "चमड़ी के नीचे"],
    "SL": ["sl", "sublingual", "under the tongue", "jeebh ke neeche", "जीभ के नीचे"],
    "PO": ["po", "oral", "orally", "by mouth", "chewed", "chew", "muh se", "मुंह से", "chabaya", "chaba ke"],
    "IN": ["in", "intranasal", "nasal", "naak se", "नाक से"],
    "INH": ["inh", "inhaled", "nebulized", "nebulised", "neb", "nebulizer", "nebulisation", "inhaler", "mdi", "nebulization"],
    "ET": ["et", "endotracheal"],
    "PR": ["pr", "rectal", "rectally"],
    "TOP": ["topical", "topically"],
}

UNITS: dict[str, list[str]] = {
    "mg": ["mg", "milligram", "milligrams", "mgs", "मिलीग्राम", "एमजी"],
    "mcg": ["mcg", "µg", "ug", "microgram", "micrograms", "माइक्रोग्राम"],
    "g": ["g", "gm", "gram", "grams", "ग्राम"],
    "mL": ["ml", "milliliter", "millilitre", "milliliters", "millilitres", "cc", "एमएल"],
    "L": ["l", "liter", "litre", "liters", "litres", "लीटर"],
    "L/min": ["l/min", "lpm", "liters per minute", "litres per minute", "liter per minute", "litre per minute", "लीटर प्रति मिनट"],
    "units": ["unit", "units", "iu", "यूनिट"],
    "mEq": ["meq"],
    "puffs": ["puff", "puffs"],
    "tablet": ["tab", "tabs", "tablet", "tablets", "गोली"],
}

# canonical procedure -> (local code, synonyms)
PROCEDURES: dict[str, tuple[str, list[str]]] = {
    "IV cannulation": ("PROC-IV", ["iv access", "iv line", "iv cannula", "cannulation", "gauge", "16g", "18g", "20g", "22g", "24g", "iv started", "started iv", "venous access", "peripheral iv", "drip lagaya", "cannula lagaya", "कैनुला"]),
    "IO access": ("PROC-IO", ["io access", "intraosseous access", "io needle"]),
    "CPR": ("PROC-CPR", ["cpr", "chest compressions", "compressions", "cardiopulmonary resuscitation", "सीपीआर"]),
    "Defibrillation": ("PROC-DEFIB", ["defibrillation", "defib", "shock", "shocked", "aed", "aed shock", "jhatka", "झटका"]),
    "Endotracheal intubation": ("PROC-ETI", ["intubation", "intubated", "et tube", "ett", "endotracheal tube"]),
    "Supraglottic airway": ("PROC-SGA", ["i-gel", "igel", "lma", "supraglottic", "king airway"]),
    "Bag-valve-mask ventilation": ("PROC-BVM", ["bvm", "bag valve mask", "ambu", "ambu bag", "bagging", "bagged"]),
    "Oxygen therapy": ("PROC-O2", ["oxygen", "o2 started", "nasal cannula", "non-rebreather", "nrb", "face mask oxygen", "ऑक्सीजन", "o2"]),
    "Suction": ("PROC-SUCT", ["suction", "suctioned", "suctioning"]),
    "Cervical collar": ("PROC-CCOL", ["c-collar", "c collar", "cervical collar", "neck collar"]),
    "Splinting": ("PROC-SPLINT", ["splint", "splinted", "splinting", "sam splint", "patta"]),
    "Tourniquet": ("PROC-TQ", ["tourniquet", "tq"]),
    "Wound dressing": ("PROC-DRESS", ["dressing", "bandage", "bandaged", "pressure dressing", "patti", "पट्टी"]),
    "12-lead ECG": ("PROC-ECG12", ["12 lead", "12-lead", "ecg", "ekg", "twelve lead"]),
    "Cardiac monitoring": ("PROC-MON", ["cardiac monitor", "monitor attached", "leads attached"]),
    "Glucose check": ("PROC-BGL", ["glucose check", "sugar check", "glucometer", "bgl", "cbg", "rbs", "sugar test"]),
    "Vagal maneuver": ("PROC-VAGAL", ["vagal maneuver", "vagal manoeuvre", "valsalva", "vagal"]),
    "Spinal immobilization": ("PROC-SPINE", ["spine board", "spinal board", "backboard", "immobilization", "immobilisation"]),
    "Needle decompression": ("PROC-NDC", ["needle decompression", "needle thoracostomy"]),
    "Cardioversion": ("PROC-CV", ["cardioversion", "synchronized cardioversion"]),
}

# Convert between mass units to compare against max dose.
MASS_TO_MG = {"mcg": 0.001, "mg": 1.0, "g": 1000.0}
