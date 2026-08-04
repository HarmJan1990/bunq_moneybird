# bunq → Moneybird sync

Synchroniseert transacties van je bunq-rekeningen naar Moneybird als
bankafschriften — als vervanging van de opgeheven rechtstreekse koppeling.
Ondersteunt meerdere bedrijven (meerdere bunq API keys en meerdere
Moneybird-administraties) vanuit één configuratie.

**Hoe het werkt:** de tool haalt via de bunq API alle nieuwe betalingen op en
maakt daar per rekening een bankafschrift (financial statement) met mutaties
van aan in Moneybird, via de officiële Moneybird API. Een lokaal statebestand
onthoudt per rekening de laatst verwerkte transactie, dus dubbele imports zijn
uitgesloten — ook als je de sync elk uur draait.

## Installatie

Vereist Python 3.10+.

```bash
git clone <deze-repo>
cd bunq_moneybird
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Configuratie

### 1. Moneybird-tokens (per administratie)

Moneybird geeft API-tokens tegenwoordig per administratie uit, dus je maakt
er één per bedrijf. Ga in de betreffende administratie naar de
API-instellingen en maak een token voor eigen gebruik aan (géén
OAuth-applicatie — als er om een callback URL gevraagd wordt zit je in het
verkeerde formulier).

```bash
export MONEYBIRD_TOKEN_BEDRIJF_1="token-van-administratie-1"
export MONEYBIRD_TOKEN_BEDRIJF_2="token-van-administratie-2"
```

Let op: de bunq-koppeling in Moneybird moet **uitgeschakeld** zijn voor de
betreffende rekening (dat is 'ie nu toch al); op een rekening met actieve
bankkoppeling kun je via de API geen afschriften aanmaken.

### 2. bunq API keys

Maak per bedrijf een API key aan in de bunq-app:
*Profiel → Beveiliging & Instellingen → Developers → API-sleutels*.

```bash
export BUNQ_API_KEY_BEDRIJF_1="sleutel-van-bedrijf-1"
export BUNQ_API_KEY_BEDRIJF_2="sleutel-van-bedrijf-2"
```

bunq koppelt een API key standaard aan het IP-adres waarvandaan hij voor het
eerst gebruikt wordt. Draait de sync op wisselende IP-adressen, zet dan
`wildcard_ip: true` in de config **vóór het eerste gebruik** van de key.

### 3. config.yaml

```bash
cp config.example.yaml config.yaml
```

Vul de ids in met behulp van deze twee hulpcommando's:

```bash
# Moneybird: administratie-ids en financial-account-ids
bunq-moneybird list-moneybird

# bunq: IBAN's per bedrijf (maakt bij de eerste keer automatisch de
# API-installatie en apparaatregistratie aan)
bunq-moneybird list-bunq --company bedrijf-1
```

## Gebruik

```bash
# Eerst kijken wat er zou gebeuren:
bunq-moneybird sync --dry-run

# Echt synchroniseren (alle bedrijven):
bunq-moneybird sync

# Eén bedrijf:
bunq-moneybird sync --company bedrijf-1
```

Bij de eerste sync van een rekening worden transacties van de afgelopen 30
dagen opgehaald (instelbaar via `initial_sync_days`); daarna alleen wat nieuw
is sinds de vorige run.

**Dubbele imports worden actief voorkomen.** Elke mutatie die deze tool
aanmaakt krijgt een code `bunq-<payment-id>` mee, waarmee hij exact
herleidbaar is naar één bunq-transactie. Vóór het aanmaken van een
afschrift haalt de sync de bestaande mutaties van die rekening en periode
uit Moneybird op en slaat alles over wat er al staat: op code (exact)
voor eigen mutaties, en heuristisch (datum + bedrag +
tegenrekening-IBAN, met aantallen) voor mutaties zonder code, zoals die
van de oude bunq-koppeling. Al geïmporteerde transacties worden dus
overgeslagen, en transacties die de oude koppeling gemist heeft worden
alsnog aangevuld.

Met `sync --rescan 30` wordt de afgelopen 30 dagen opnieuw met Moneybird
vergeleken, ongeacht het onthouden syncpunt; alleen wat ontbreekt wordt
aangevuld. Handig als er ooit iets gemist lijkt te zijn.

Wil je bij de eerste sync verder terug (of juist minder ver) dan de
standaard 30 dagen, zet dan per rekening een startdatum:

```yaml
      - iban: NL00BUNQ0000000000
        moneybird_financial_account_id: "..."
        sync_from: "2026-01-01"
```

Controleer het altijd eerst met `sync --dry-run`; die laat ook zien hoeveel
transacties als al-bestaand worden overgeslagen. `sync_from` geldt alleen
voor de allereerste sync van een rekening; daarna bepaalt het statebestand
waar verdergegaan wordt.

## Uitbetalingen klaarzetten (`pay`)

Naast het synchroniseren naar Moneybird kan de tool een
SEPA-uitbetalingsexport (CSV of pain.001.001.03-XML, zoals de
WijKopenBonnen-export) inlezen en als **concept-betaling** in bunq
klaarzetten:

```bash
# Eerst controleren wat er in het bestand zit:
bunq-moneybird pay wijkopenbonnen-uitbetalingen-20260802-181422.xml \
    --company bedrijf-1 --dry-run

# Echt klaarzetten:
bunq-moneybird pay wijkopenbonnen-uitbetalingen-20260802-181422.xml \
    --company bedrijf-1
```

Veiligheidsontwerp, bewust zo gekozen:

- **De tool maakt nooit zelf geld over.** Er wordt een draft payment
  aangemaakt die jij in de bunq-app moet goedkeuren; pas dan wordt er
  betaald. Tot die tijd kun je hem in de app ook gewoon weggooien.
- **Elke referentie wordt maar één keer ingediend.** Al ingediende
  referenties (bijgehouden in `.state/payouts.json`) worden overgeslagen,
  dus dezelfde export twee keer draaien kan geen dubbele uitbetaling
  veroorzaken.
- **Het bestand wordt streng gevalideerd** voordat er iets gebeurt:
  IBAN-controlegetallen (mod-97), positieve bedragen met maximaal 2
  decimalen, alleen EUR, unieke referenties, en bij XML moeten `NbOfTxs`
  en `CtrlSum` kloppen met de daadwerkelijke inhoud.

Bij een XML-export wordt de rekening waarvan betaald wordt uit het bestand
gehaald (`DbtrAcct`); bij CSV geef je hem op met `--iban`. Grote exports
worden gesplitst in concept-betalingen van maximaal 100 uitbetalingen.

### Automatisch draaien (cron)

```cron
# Elk uur synchroniseren
0 * * * * cd /pad/naar/bunq_moneybird && .venv/bin/bunq-moneybird sync >> sync.log 2>&1
```

Zet de omgevingsvariabelen dan bijv. in een `.env`-bestand dat je in het
cronscript sourcet (`.env` staat al in `.gitignore`).

## Bestanden die lokaal blijven

| Pad | Inhoud |
|---|---|
| `config.yaml` | jouw configuratie (ids, geen geheimen) |
| `.bunq/*.json` | bunq API-context per bedrijf (RSA-sleutel + sessietokens) |
| `.state/sync-state.json` | laatst verwerkte transactie per rekening |

Alle drie staan in `.gitignore`. De bunq-contextbestanden bevatten gevoelig
materiaal; ze worden met bestandsrechten `600` weggeschreven.

## Technische details

- De bunq-client implementeert zelf de officiële API-flow (installation →
  device-server → session-server) met RSA-request-signing, zonder de
  verouderde bunq-SDK. Verlopen sessies worden automatisch vernieuwd.
- Afschriften krijgen een referentie als `bunq NL00BUNQ0000000000 #1234-1301`
  (de bunq payment-id-range), zodat je in Moneybird altijd kunt herleiden wat
  waar vandaan komt.
- Per 100 mutaties wordt een apart afschrift aangemaakt; het statebestand
  wordt na elk afschrift bijgewerkt, dus een afgebroken run kan veilig
  opnieuw gestart worden.
- Testen kan tegen de bunq-sandbox met `sandbox: true` in `defaults`.
