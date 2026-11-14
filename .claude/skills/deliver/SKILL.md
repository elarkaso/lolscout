---
name: deliver
description: Dodá novou funkci nebo větší změnu lolscout od návrhu po review – architect → developer → verify → reviewer → opravy. Pro drobné opravy stačí developer + /verify.
argument-hint: <popis funkce a omezení>
disable-model-invocation: true
---

Dodej tuto změnu v projektu lolscout:

$ARGUMENTS

1. **Rozsah.** Přečti `CLAUDE.md` a dotčené části `lolscout.py`. Sepiš pozorovatelná akceptační kritéria (co uživatel uvidí v reportu/CLI). Pokud jde o lokální změnu v jedné funkci bez nového endpointu a beze změny CLI, přeskoč krok 2.
2. **Návrh.** Spusť agenta `architect`. Otevřená rozhodnutí, která mění veřejné CLI/formát reportu, přidávají závislost nebo by vyžadovala production klíč, předlož uživateli (max. 2–3 možnosti s doporučením). Ostatní rozhodni sám podle konvencí.
3. **Implementace.** Spusť agenta `developer` s akceptačními kritérii a návrhem.
4. **Ověření.** Skill `verify`. Selhání vrať developerovi.
5. **Review.** Spusť agenta `reviewer` nad celým diffem. Nálezy BLOCKING a IMPORTANT vrať developerovi, po opravě zopakuj kroky 4–5.
6. **Předání.** Shrň: co se změnilo, výsledky ověření, dopad na počet API požadavků, aktualizace README, otevřené body. Necommituj, pokud o to uživatel nepožádal.
