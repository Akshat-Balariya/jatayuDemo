# Field-to-Record — Pitch Kit

Slide deck: https://claude.ai/artifact/3856xnqwpDvAFncFjdvjEq (private until shared from its Share menu)

## USP in one line

**An offline, Hinglish-native voice scribe that checks every AI suggestion against the paramedic’s own words and makes the record tamper-evident from the first minute.**

## 60-second pitch

> When an ambulance crew is saving a life, nobody is writing anything down. Drugs, doses and times get reconstructed later, from memory — and that note is the **first link in the patient’s medical record**. If it’s wrong, everything after it is built on a mistake.
>
> **Field-to-Record lets the paramedic just speak** — in English, Hindi or Hinglish, the way crews actually talk: *“das minute pehle Dolo 650 diya, bees gauge cannula lagaya.”* Seconds later, it’s a structured record: paracetamol 650 mg by mouth with an exact timestamp, IV cannulation 20 gauge.
>
> Three things make it different.
>
> **It works with zero internet.** Speech recognition and AI run entirely on the device — in an ambulance, a basement, a highway with no signal. No patient data ever leaves it.
>
> **It doesn’t blindly trust the AI.** Every drug and dose must be backed by the paramedic’s actual words. If the AI mislabels a drug, our rules catch it and correct it from the transcript. Dangerous doses get flagged. Nothing is saved until the paramedic confirms.
>
> **It’s tamper-proof.** Every signed record is cryptographically chained to the one before it. If anyone changes a dose afterwards — even directly in the database — the system shows exactly which record was altered.
>
> Speak the care, keep the record. Accurate, offline, and trustworthy from the very first minute.

## Why it stands out

| Most AI scribes | Field-to-Record |
|---|---|
| Need the cloud | Fully offline, on-device |
| English-first | Hinglish, Devanagari, Indian brand names (Dolo, Ondem, Avil) |
| Trust the AI’s output | Every item must quote the transcript; wrong drugs auto-corrected, doses sanity-checked |
| Produce an editable note | Produce a signed, hash-chained, tamper-evident record |
| Built for the doctor’s desk | Built for the field: noisy, rushed, mixed-language, no signal |

## Likely questions

- **“What if the AI hallucinates?”** — It can’t sneak anything in: every item must quote the transcript, and a human signs. We hit a real case in testing (Dolo mislabelled as a different drug) and the system now corrects it automatically.
- **“How accurate is it?”** — On 26 labelled test notes it gets every drug, dose and route right. Those are synthetic notes and the rules were tuned on them; real paramedic recordings are next.
- **“Why not just use ChatGPT?”** — No internet in half the places ambulances go, patient data can’t go to the cloud, and a chatbot gives you no audit trail.
- **“How do you share a record with a hospital without internet?”** — The ambulance arrives with the patient, so the record travels with it: today as a printed report; planned as a QR code at handover (scan → structured record + integrity check), local hotspot/Bluetooth transfer, and store-and-forward sync when a connection returns. Offline authenticity needs per-device Ed25519 signatures (roadmap).

## Story (no AI jargon)

### 9:42 PM — NH-48

The call comes in: a 58-year-old man collapsed at a roadside dhaba, chest pain, sweating heavily.

Ravi, the paramedic, kneels beside him. There’s no time for paperwork. While his partner sets up the monitor, Ravi works and talks, the way he always does:

*“Fifty-eight saal ke uncle, seene mein dard aadhe ghante se. Disprin 300 chaba ke diya. Sorbitrate jeebh ke neeche rakha. Bees gauge cannula left hand mein. BP 150 by 95.”*

But this time, someone is listening — the laptop strapped into the ambulance.

There’s no signal on this stretch of highway. There never is. It doesn’t matter.

By the time they lift the patient onto the stretcher, the screen shows a clean list:
- **Aspirin 300 mg**, by mouth, 9:44 PM
- **Isosorbide dinitrate 5 mg**, under the tongue, 9:45 PM
- **IV cannula, 20 gauge**, left hand
- **BP 150/95**

Next to each line, in small grey text, are Ravi’s own words it came from.

One line is highlighted in orange. Ravi glances at it — he’d said “Sorbitrate” without a dose. He taps in 5 mg, ticks the box, and presses **Sign**.

### 10:21 PM — City Hospital, Emergency

Dr. Meera has thirty seconds for handover. Ravi hands her a single printed page.

No scribbled handwriting. No “how much aspirin, and when?” Every drug, dose, route and time is there, down to the minute. At the bottom, a line notes that one dose was entered by hand by the paramedic.

She orders the ECG and moves on. Nothing is given twice. Nothing is missed.

### Three weeks later — Quality review

The hospital’s audit team pulls the case. They don’t need to trust anyone’s memory. The record shows exactly what Ravi said, word for word; what was written down; the one field he changed; and proof that nobody touched it after he signed.

If anyone had quietly edited “300 mg” to something else, the record would point straight to where it was broken.

### Closing line

> **Ravi never stopped treating his patient to write a note. And for the first time, the note was the most reliable part of the night.**

*Scope note: everything in the story is built today. Handover uses the printed report; QR handover is planned, not built.*
