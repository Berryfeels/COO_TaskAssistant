┌───────────────────────────┬────────────────┬─────┬──────────────────────────────┐
│ Requisito blueprint       │ Dove nel       │ Sta │ Nota                         │
│                           │ codice         │ to  │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ FIFO per utente,          │ db.queue_push/ │ OK  │                              │
│ ingestione sola           │ pop,           │     │                              │
│                           │ Pipeline.drain │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Idempotenza               │ pipeline._proc │ OK  │ semantica §3.1 rispettata    │
│ mark_processed dopo write │ ess            │     │                              │
│ Trello / dopo reply query │                │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Conferme legate a         │ PendingService │ OK  │ write ora immediati          │
│ pending_id, "OK" ambiguo  │ .match_confirm │     │ (decisione 2026-10-29),      │
│ → elenco                  │ ation          │     │ pending drenati da legacy    │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Near-duplicate guard,     │ pipeline._simi │ OK  │                              │
│ solo add_card, ratio      │ lar_cards,     │     │                              │
│ 0.75, solo heads-up       │ similarity.py  │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ LLM non gestisce ID;      │ parser.py      │ OK  │ è il cambio richiesto: LLM   │
│ risoluzione nomi→ID       │ (nuovo)        │     │ dà le parole, il parser      │
│ deterministica            │                │     │ riempie il JSON              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ LLM produce "tool call    │ mistral_llm.py │ PAR │ per le écriture il LLM ora   │
│ con nomi come detti       │                │ ZIA │ manda parole, non argomenti  │
│ dall'utente" (§2.1 step   │                │ LE  │ di tool; i read tool restano │
│ 3)                        │                │     │ function-calling. Invariante │
│                           │                │     │ chiave (mai ID inventati)    │
│                           │                │     │ rispettata                   │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Catalogo tool caricato a  │ _TOOLS         │ GAP │ actions_list.txt non è       │
│ runtime da                │ hardcoded in   │     │ caricato a runtime (e non è  │
│ actions_list.txt (§2.2,   │ mistral_llm.py │     │ in formato JSON schema)      │
│ §4.1)                     │                │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Rate limit + retry su     │ TrelloClient._ │ GAP │ nessun retry/backoff sulle   │
│ Trello (§2.3, §4.4)       │ req            │     │ chiamate                     │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Cache struttura board con │ assente        │ GAP │ il parser legge Trello live  │
│ TTL + refresh on miss     │                │     │ a ogni lookup (comportamento │
│ (§2.4)                    │                │     │ equivalente, ma il           │
│                           │                │     │ meccanismo di cache del      │
│                           │                │     │ blueprint non esiste; lists  │
│                           │                │     │ in DB è solo il mirror delle │
│                           │                │     │ colonne create)              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ History 12 turni con id   │ MAX_HISTORY=12 │ PAR │ la history salva solo i      │
│ ultima card modificata    │                │ ZIA │ messaggi, non l'id           │
│ (§3.2)                    │                │ LE  │ dell'ultima card ("assegnalo │
│                           │                │     │ a Marco" non risolvibile)    │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Scheduler ogni 10 min su  │ run_bot.schedu │ GAP │ scheduler è orario (3600s) e │
│ scadenze card 24h/1h +    │ ler            │     │ fa solo reminder/expiry dei  │
│ reminders_sent (§2.1 step │                │     │ pending; il monitoraggio     │
│ 7, §4.4)                  │                │     │ scadenze delle card su       │
│                           │                │     │ Trello non è implementato    │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Webhook non implementati  │ nessun codice  │ OK  │ coerente con lo stato "not   │
│ (§4.3)                    │ webhook        │     │ yet implemented"             │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Auth key+token query      │ TrelloClient._ │ OK  │                              │
│ param, vuoti scartati; id │ req, .board_id │     │                              │
│ board 24 char lazy per    │                │     │                              │
│ /search e POST /lists     │                │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Onboarding gate → tabella │ onboarding.py, │ OK  │                              │
│ users                     │ db.users       │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Date Europe/Berlin → ISO  │ parser._parse_ │ OK  │ Trello converte in UTC       │
│ con offset verso Trello   │ due, dates.py, │     │                              │
│                           │ config.timezon │     │                              │
│                           │ e              │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Lingua: reply nella       │ SYSTEM_PROMPT  │ OK  │                              │
│ lingua utente, card in    │                │     │                              │
│ inglese                   │                │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Gestione errori: write    │ pipeline._proc │ OK  │                              │
│ Trello fallita → errore   │ ess, builder,  │     │                              │
│ chiaro; LLM fallisce →    │ simplex.on_cha │     │                              │
│ reply, nulla scritto;     │ t_event        │     │                              │
│ gateway logga e continua  │                │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Migrazione                │ db.__init__,   │ OK  │                              │
│ task_json→card_json, DB   │ run_bot.py     │     │                              │
│ persistente, .env,        │                │     │                              │
│ shutdown SIGINT pulito    │                │     │                              │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Notification builder per  │ notify/builder │ PAR │ la risposta alle query è     │
│ tutti i messaggi in       │ .py            │ ZIA │ costruita inline in          │
│ uscita                    │                │ LE  │ pipeline._answer_query       │
│                           │                │     │ invece che dal builder       │
├───────────────────────────┼────────────────┼─────┼──────────────────────────────┤
│ Audio → trascrizione      │ gateway scarta │ OK  │ marcato "not yet             │
│ prima del LLM             │ non-text       │     │ implemented" in entrambi     │
└───────────────────────────┴────────────────┴─────┴──────────────────────────────┘
