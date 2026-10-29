# Open500 Link — protocollo MPC500 ↔ schermo remoto / companion app

Stato (Open500 0.2, 2026-10-29): implementati VIEW (§4.2), CAPS (§4.1), ACTION (§4.3, vista di sistema MIX), cassetta
(§6) e le viste del §5. Trasporti provati sul ferro: MIDI DIN (add-on MIDI Screen) e USB-MIDI (add-on USB MIDI).
Ricevitore di riferimento: `docs/o5link.js` (usato da `docs/screen.html`). Note di sviluppo, in italiano.
Obiettivo: un protocollo unico per MIDI DIN, USB-MIDI e companion app (iOS/Android/web), così che **ogni add-on pubblichi
la sua vista una volta** e qualsiasi ricevitore la mostri e la comandi.

## 1. Principi
- **Dati semantici, non pixel.** L'MPC manda lo stato (note di una griglia, tagli, parametri); il ricevitore disegna.
- **SysEx `F0 7D … F7`** (0x7D = manufacturer "non commerciale") su ogni trasporto: DIN e USB-MIDI trasportano gli stessi
  messaggi. Nessun formato diverso per l'USB.
- **Mai bloccare l'OS.** L'MPC scrive nella coda TX solo se c'è posto per tutto il pacchetto, a interrupt bloccati,
  senza attese (come MIDI Screen v7). Il MIDI clock dell'OS non va mai ritardato.
- **Retrocompatibile.** I messaggi esistenti (§3) restano validi; i nuovi usano codici liberi.
- **Il ricevitore è stupido e tollerante**: butta i pacchetti che non capisce, ricompone i pezzi, si aggiorna da solo
  grazie ai reinvii periodici (chi si collega dopo vede tutto entro pochi secondi).

## 2. Trasporti
| Trasporto | Banda utile | Note |
|---|---|---|
| MIDI DIN OUT/IN | 3125 B/s, noi ≤ ~30% | un pacchetto ogni ≥ 35 ms; condiviso con note e clock veri |
| USB-MIDI (add-on USB MIDI, class compliant "MPC500 MIDI") | molto più alta, da misurare (dipende dal polling a ogni fire) | pacchetti USB-MIDI da 4 byte (CIN 4/5/6/7 per SysEx); niente clock da proteggere |
| Futuro: ESP32/BLE su DIN | come DIN | ponte trasparente |

Regola di banda: il **trasmettitore** (oggi MIDI Screen, domani anche USB MIDI) sceglie il ritmo in base al trasporto;
gli add-on non sanno su cosa viaggiano i loro dati.

## 3. Messaggi esistenti (legacy, implementati e provati su HW)
Codifica numeri: `5x7` = 35 bit little-endian in 5 byte da 7 bit; `4x7`, `2x7` idem.

| Cmd | Direzione | Formato | Chi |
|---|---|---|---|
| 01 DISPLAY | MPC→ | `F0 7D 01 seq cursore+1 <32 char> F7` | MIDI Screen (copia LCD, vale per ogni pagina e add-on) |
| 02 WAVE | MPC→ | `F0 7D 02 sig chunk <32 picchi> F7`, 8 chunk = 256 colonne | MIDI Screen |
| 03 TRIM | MPC→ | `F0 7D 03 sig len5x7 start5x7 end5x7 F7` | MIDI Screen |
| 04 CHOP | MPC→ | `F0 7D 04 sig mseq parte n k <k pos 4x7> F7` | MIDI Screen, dati dalla cassetta di Trans-Chop |
| 05 PLAY | MPC→ | `F0 7D 05 sig cnt on fetta+1 start5x7 end5x7 F7` | MIDI Screen |
| 10/11 PING | ↔ | `F0 7D 10 id F7` → `F0 7D 11 id ver F7` | MIDI Screen v7 (MIDI IN non verificato su HW: cavo clone) |
| 12/13 PEEK | ↔ | `addr5x7 n≤32` → `n <2n nibble>` | idem |
| 14/15 POKE | ↔ | `addr5x7 n <2n nibble>` → ack | idem |
| 16/17 EVENT | ↔ | `type2x7 code2x7 data5x7` → `post_event` dell'OS | idem |
| 20 HELLO | MPC→ | `F0 7D 20 n F7` ogni 0,5 s | USB MIDI (prova) |

`sig` = firma del sample corrente: quando cambia il ricevitore butta i dati vecchi.

## 4. Nuovi messaggi (v1)
Codici riservati: `0x00-0x0F` schermo legacy · `0x10-0x1F` richiesta/risposta · `0x20-0x2F` annunci ·
`0x30-0x3F` **viste** · `0x40-0x4F` **azioni** · `0x50-0x5F` trasferimenti grossi (futuro).

**Codifica binaria nuova: "packed7"** (standard MIDI 8→7): ogni gruppo di 7 byte diventa 1 byte con i 7 bit alti + 7 byte
coi 7 bit bassi (+14% invece del +100% dei nibble). **Checksum** `chk` = XOR di tutti i byte dopo `7D` fino a prima di
`chk`, `& 0x7F`. I pacchetti con chk sbagliato si buttano (e si contano: utile su DIN).

### 4.1 CAPS — cosa c'è caricato
`F0 7D 18 id chk F7` → `F0 7D 19 id proto n <n × (vista, ver)> chk F7` (solo viste vive; nomi: futuro)
Permette all'app di mostrare solo le schede degli add-on presenti. `proto` = 1.

### 4.2 VIEW — stato di un add-on (MPC→)
`F0 7D 30 vista ver seq parte nparti <payload packed7, ≤ 48 byte grezzi> chk F7`
- `vista` = tipo registrato (§5), `ver` = versione del suo formato, `seq` = 0..127, cambia quando cambia lo stato.
- Payload lunghi divisi in `nparti` pezzi; il ricevitore aggiorna la vista solo quando ha tutte le parti dello stesso `seq`.
- Il trasmettitore manda una vista **quando il suo seq cambia** e la ripete ogni ~2 s (DIN) / ~0,5 s (USB).
- Parti da **40 byte grezzi** (pacchetto ≤ 55 byte). Se il seq cambia a metà invio si riparte dalla parte 0.
- Una vista è **viva** se il suo battito (+0x0C) è più recente di 2 s: add-on cancellato = vista non più mandata.
- Priorità: viste della pagina aperta prima delle altre.

### 4.3 ACTION — comando verso un add-on (→MPC) — v11: implementata per la vista di sistema 10 (MIX)
`F0 7D 40 id vista azione arg 5x7 chk F7` → `F0 7D 41 id esito chk F7` (esito 0 ok, 1 vista assente, 2 coda piena, 3 rifiutata)
- Il trasmettitore mette l'azione nella **casella di posta** della vista (§6); l'add-on la consuma al suo fire.
- Azioni generiche (tutte le viste): `00` richiesta reinvio immediato, `01` apri la pagina dell'add-on.
- Le altre sono definite per vista (§5). Tasti/rotella grezzi restano su EVENT (16).

## 5. Viste registrate
Numero = anche indice della casella in RAM (§6). Byte salvo indicazione; maschere little-endian.

| N | Vista | Add-on | Payload (v1) | Azioni |
|---|---|---|---|---|
| 1 | CHOP | Auto Chop, Trans-Chop, Auto Warp (→ Chop Store) | sig, n, pos u32[n≤64], fetta in ascolto | 10 sposta taglio (idx, pos), 11 aggiungi, 12 togli, 13 ascolta fetta |
| 2 | GRID | Grid Seq (fatto) | u16 battute, battuta, step, pad scelto, banco, modo, traccia, 12 × u16 colpi della battuta (pad del banco); ogni 8 fire a pagina aperta | 10 toggle (pad, step), 11 battuta, 12 velocity (pad, step, v) |
| 3 | SCALE | Scale Pads (fatto) | scala, tonalità, banco, stato (0/1/2), pad sorgente (0xFF nessuno), 12 semitoni s8 | 10 scala, 11 tonalità, 12 banco, 13 esegui |
| 4 | MUTE | Pad Mute (fatto) | banco (0-3), poi 48 byte, uno per pad: bit0 mute, bit1 solo, bit2 zittito | 10 toggle mute (pad), 11 toggle solo (pad), 12 tutto on |
| 5 | MFX | Master FX | effetto per pad[12], pad tenuti mask u16, slider, bpm×10 u16, parametri attivi | 10 tieni/rilascia pad, 11 slider (0..127), 12 XY (x, y) |
| 6 | SFX | Sample FX | catena[8] × (effetto, param), sample sig | 10 imposta slot, 11 esegui |
| 7 | PADS | **MIDI Screen (sistema, v11)** | program u16 + 48 × nome8 (layer 0, se vuoto layer 1) | — |
| 8 | THRU | Input Thru | on, livello | 10 on/off, 11 livello |
| 9 | SEQ | Seq Tools (fatto) | battute u16, eventi u16 | 10 double, 11 half |
| 10 | MIX | **MIDI Screen (sistema, v11)** | program u16, banco, 48 × livello (0..100) | 10 livello: arg = livello \| pad<<7 \| program<<13 (rifiutata se il program non è più quello) |
| 11 | MEM | **MIDI Screen (sistema, v11)** | usati u32, totali u32, n sample u16, frame del corrente u32, nome16 del corrente | — |
| 12-23 | liberi | nuovi add-on / terze parti (via SDK) | | |

**Viste di sistema (v11, 2026-10-17)**: le genera MIDI Screen dalla memoria dell'OS, al posto delle letture PEEK che
l'app faceva da sé (indirizzi dell'OS nell'app + traffico continuo). Sono pubblicate **solo con un'app collegata**: un
CAPS negli ultimi 10 s (l'app lo rimanda ogni ~4 s). PEEK/POKE restano per gli strumenti di sviluppo.

**Porte (MIDI Screen v10)**: la risposta a un comando va **solo alla porta da cui è arrivato** (MIDI IN DIN o USB);
con il cavo USB attivo lo schermo (display, viste...) va **solo su USB** e la MIDI OUT DIN resta libera. Un comando
nuovo resta in coda finché la risposta precedente non è partita (prima veniva letto e scartato).

Le viste "di sistema" restano i legacy 01-05 (display, forma d'onda, trim, ascolto), generati dal trasmettitore
senza bisogno di add-on.

## 6. Lato MPC: la cassetta delle viste (RAM)
Area libera già riservata dal loader: **B+0x58000 .. B+0x5F000** (28 KB fra gli slot addon e la vecchia cassetta di MIDI Screen).
```
B+0x58000  intestazione  +0 'O5V1'  +4 versione  +8 vista della pagina aperta (0 = nessuna)
B+0x58000 + N*0x400  casella della vista N (N = 1..23, 1 KB ciascuna)
   +0x000 seq   (u32, scritto per ULTIMO dall'add-on: il trasmettitore copia solo se seq e' stabile)
   +0x004 len   (byte di payload)  +0x008 ver  +0x00C battito (contatore frame dell'OS all'ultimo publish)
   +0x010 payload (fino a 0x3B0 byte)
   +0x3C0 posta: 4 azioni × 16 byte {id, azione, arg, stato} (scritte dal trasmettitore, consumate dall'add-on)
```
La vecchia cassetta di Trans-Chop (B+0x5F000, 'SCR1') resta finché la vista CHOP non la sostituisce.

**SDK** (da fare, `sdk/view.py`):
- `v = View(pg, VIEW_GRID, ver=1)` — riserva la casella.
- `v.publish(m, src_reg, nbytes)` — copia il payload e incrementa seq (una chiamata nel fire, solo se cambiato).
- `v.on_action(code, fn)` — gestore asm per un'azione; l'SDK legge la posta a ogni fire.
Gli add-on non sanno nulla di MIDI/USB: pubblicano e ricevono azioni.

## 7. Lato ricevitore (sito / app)
- Un solo modulo JS "o5link": parsing SysEx, ricomposizione parti, viste tipizzate, invio azioni con id e ritentativi.
- Lo usano `screen.html` (sito), l'app iOS/Android (stesso codice in un guscio nativo + ponte CoreMIDI / Android MIDI)
  e gli script Python (`mpc_remote.py`).
- Ogni vista = un componente (griglia, tastiera, pad…). Una vista sconosciuta si ignora.

## 8. Ordine di lavoro proposto
1. `sdk/view.py` + cassetta (§6) + VIEW/CAPS nel trasmettitore MIDI Screen (DIN, già provato su HW).
2. Prima vista nuova: **MUTE** (Mute/Solo nasce compatibile), poi SCALE e GRID.
3. ACTION + posta: richiede MIDI IN funzionante → provarlo via **USB MIDI** (il DIN IN è bloccato dal cavo clone).
4. Trasmettitore anche su USB MIDI (stessi messaggi).
5. Modulo JS o5link + `screen.html` v2 con le schede; poi guscio app.

## 9. Domande aperte
- Banda USB reale con il polling a ogni fire (~300/s): da misurare prima di fissare i ritmi.
- Convivenza USB MIDI con la modalità disco dell'OS (oggi: USB MIDI ON = la porta è nostra).
- Le azioni che modificano il program/sequenza (GRID toggle, SCALE esegui) vanno eseguite **nel fire dell'add-on**, mai
  dall'interrupt: stesse regole di sicurezza di oggi (interrupt bloccati solo per gli spostamenti di memoria).
- Terze parti: i numeri di vista 10-23 vanno assegnati da noi (registro nel repo) per evitare collisioni.
