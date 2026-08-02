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
