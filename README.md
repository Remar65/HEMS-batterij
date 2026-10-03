# HEMS Batterij

Eigen Home Assistant-integratie voor de aansturing van de twee Marstek Venus E-thuisbatterijen
(M1 = V2, M2 = V3, via Modbus op LilyGo/ESPHome), als opvolger van HomeBatteryControl (HBC).

> **Versie 0.1 kijkt alleen mee.** De integratie leest prijzen, zon, P1 en de batterijen,
> maakt een plan en rekent uit wat zij zou doen. Er zit geen code in die iets naar de
> batterijen stuurt; HBC blijft gewoon de baas. Een test (`tests/test_no_writes.py`) en een
> integratietest die alle service-aanroepen controleert, bewaken dat.

## Wat het doet

**Planner (elk kwartier en zodra er nieuwe prijzen zijn).** Over alle bekende
Zonneplan-kwartierprijzen zoekt de planner het goedkoopste schema voor de twee batterijen
samen. Hij rekent met:

- inkoopprijs per kwartier, en terugleverprijs (tot het einde van de saldering gelijk aan de
  inkoopprijs, daarna de kale prijs; datum en aan/uit instelbaar);
- Solcast-verwachting (halfuurs, verdeeld over kwartieren);
- verwacht huisverbruik uit een zelflerend profiel per kwartier van de week (begint op 400 W
  en leert vanaf dag één bij);
- het gemeten round-trip-rendement per batterij (lifetime-sensor, nu ca. 79,5 % en 83 %);
- slijtage per ontladen kWh (uit `input_number.batterij_slijtagekosten`, nu 4,7 ct);
- de laad/ontlaadgrenzen en SoC-grenzen die op de batterijen zelf staan.

Daarnaast geldt Marco's rangorde, als marges in dezelfde afweging (instelbaar):

- **Zon eerst** (10 ct/kWh): zon-overschot terugleveren terwijl het opgeslagen had kunnen
  worden telt als nadeel, zodat zon bijna altijd de batterij in gaat.
- **Handelsdrempel** (3 ct/kWh): laden uit en ontladen naar het net alleen als het per kWh
  minstens zoveel meer oplevert dan verliezen + slijtage. Kleine dagwinstjes vallen zo weg.

Verkopen aan het net terwijl er later die dag nog zon-overschot komt, telt even zwaar als zon
terugleveren. Anders zou "zon eerst" de planner verleiden om 's ochtends de batterij te verkopen
alleen om ruimte te maken voor de zon; nu beslist dan alleen het echte prijsverschil.

Wat aan het einde van de horizon nog in de batterij zit krijgt een restwaarde, zodat hij niet
om middernacht leeggegooid wordt omdat de prijzen van morgen nog onbekend zijn.

Elk kwartier krijgt een modus met een leesbare reden:

| Modus | Betekenis |
| --- | --- |
| `zelfverbruik` | P1 naar 0 regelen: overschot opslaan, tekort uit de batterij (eventueel met laadlimiet) |
| `net_laden` | met vast vermogen laden, ook uit het net, omdat het prijsverschil de verliezen + slijtage ruim dekt |
| `verkopen` | met vast vermogen ontladen, ook naar het net |
| `vasthouden` | niet ontladen; energie bewaren voor een duurder moment. Zon-overschot mag er wel in |
| `vangnet` | data ontbreekt of een batterij geeft een alarm: niets voorstellen |

**Snelle regellus (elke 5 seconden).** Rekent op basis van de modus en de P1-meting het
gewenste totaalvermogen uit en verdeelt dat over M1 en M2: kleine vermogens naar één batterij
(Marsteks zijn bij laag vermogen inefficiënt; bij ontladen de volste, bij laden de leegste,
bij gelijke stand die met het beste rendement), grotere vermogens naar rato van beschikbare
energie of ruimte, binnen de limieten per batterij.

**Vangnetten.** P1 ouder dan 30 s, batterijdata ouder dan 120 s, BMS-beveiliging,
communicatiefout, over/onderspanning, te warm/koud, RS485-besturing uit: die batterij doet niet mee, en zonder P1 of
zonder bruikbare batterij wordt niets voorgesteld. Ontbreekt alleen het plan (geen prijzen),
dan valt hij terug op gewoon zelfverbruik. Laden wordt teruggeschroefd als de netafname boven
16 kW (3x25A met marge) zou komen.

**Beslislog.** Elke modus-wissel en elk nieuw plan komt met reden en de HBC-strategie van dat
moment in het attribuut `beslislog` van de modus-sensor en in het HA-log.

## Entiteiten

| Entiteit | Inhoud |
| --- | --- |
| Modus | huidige modus; attributen: reden, plan (komende 12 uur), beslislog, HBC-strategie |
| Voorgesteld vermogen | totaal (+ ontladen, − laden), met verdeling per batterij |
| Voorgesteld vermogen marstek_m1 / _m2 | wat deze integratie per batterij zou instellen |
| Werkelijk vermogen (HBC) | wat de batterijen nu echt doen |
| Afwijking HBC t.o.v. voorstel | werkelijk − voorstel, om naast elkaar te leggen |
| Huisverbruik | net + zon + batterij; attribuut: hoeveel van het profiel al geleerd is |
| Energie in batterijen | kWh in beide samen, en verwachte stand aan het einde van het plan |
| Verwachte besparing plan | t.o.v. geen batterij, over de bekende prijzen |
| Netkosten vandaag | werkelijke netkosten op basis van P1 × kwartierprijs |
| Besparing batterijen vandaag | netkosten zonder batterij − met batterij (zoals HBC nu stuurt) |
| Terugverdientijd | aanschafprijs ÷ gemeten besparing per jaar (pas zinvol na een paar weken) |
| Rendement (gemeten) | gewogen round-trip-rendement en gebruikte slijtage |
| Minimale spread nu | break-even prijsverschil voor laden uit het net, in ct |
| Meekijkmodus | staat in deze versie altijd aan |
| Vangnet | aan als er iets ontbreekt of een alarm is; attribuut: redenen |

## Installeren

HACS ondersteunt geen privé-repositories. Er zijn twee manieren:

1. **Repository openbaar maken** (er staan geen wachtwoorden of sleutels in) en in HACS
   toevoegen via *Custom repositories* → `https://github.com/Remar65/HEMS-batterij`,
   type *Integration*. Updates komen dan vanzelf via HACS.
2. **Handmatig**: de map `custom_components/hems_batterij` kopiëren naar
   `/config/custom_components/` (via Samba of de File editor) en HA herstarten.

Daarna: *Instellingen → Apparaten en diensten → Integratie toevoegen → HEMS Batterij*.
De standaardwaarden zijn al ingevuld voor deze installatie (P1 van HomeWizard,
APsystems- en Enphase-kWh-meters, Zonneplan, Solcast, `marstek_m1, marstek_m2`).

## Plan van aanpak

1. **v0.1 meekijken** (deze versie): plan en voorstel naast HBC, besparing en afwijking volgen.
2. Vergelijken over een paar weken: werkelijke netkosten vs. wat het plan verwachtte.
3. **Overnemen per batterij, alleen na akkoord**: eerst M2, HBC blijft terugval voor M1.
4. Saldering per 1-1-2027: terugleverprijs daarna instellen op wat Zonneplan dan vergoedt.

## Ontwikkelen

```bash
pip install -r requirements_test.txt ruff
ruff check . && pytest -q
```

De planner, regellus, prijs- en voorspellingslogica zijn gewone Python-modules zonder
Home Assistant-afhankelijkheden en hebben eigen unit tests.
