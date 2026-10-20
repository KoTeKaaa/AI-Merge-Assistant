# Подключение LLM-модуля

Модуль получает подготовленные DTO и возвращает непроверенные артефакты. Он не читает
репозиторий, не исполняет Python, не рассчитывает покрытие и не принимает решение о мерже.
Парсер предоставляет контекст, оркестратор хранит состояние и бюджеты, шлюз проверяет
пути/артефакты, sandbox runner исполняет проверки. Общие API, очередь и БД подключаются отдельно.

## Создание сервиса

Runtime-зависимости: `backend/requirements-llm.txt`; Python 3.13. Импорт SDK нужен только
адаптеру. При импорте фасада нет чтения окружения, файлов или запросов.

```python
import os
from app.services.llm.settings import LLMSettings
from app.services.llm.gemini import GeminiProvider
from app.services.llm.service import LLMService

settings = LLMSettings.from_environment(os.environ)
service = LLMService(GeminiProvider(settings), settings)
```

Загрузку `.env` выполняет приложение. Ключи и пользовательские артефакты хранятся вне
общего Git. Настройки не выбирают модель и fake как неявный fallback.

| Переменная | Значение |
|---|---|
| `GEMINI_API_KEY` | Обязательный секрет для Gemini |
| `LLM_MODEL` | Обязательный явный ID; для простой калибровки `gemini-3.1-flash-lite` |
| `LLM_BACKEND` | `gemini` (по умолчанию) или явно `fake` |
| `LLM_STAGE_TIMEOUT_SECONDS` | 120 |
| `LLM_REQUEST_TIMEOUT_SECONDS` | 60 |
| `LLM_MAX_ATTEMPTS` | 3 на одну операцию count/generate, внутри deadline |
| `LLM_MAX_INPUT_TOKENS` | 24000 |
| `LLM_MAX_OUTPUT_TOKENS` | 8000 |
| `LLM_MAX_REPAIRS` | 2 |

Для fake приложение явно передаёт `FakeProvider` и `LLMSettings(backend="fake", model_id="...")`.
Фасад не создаёт провайдера и не переключает его по ошибке. Вызовы синхронные; постановка
в worker и отмена задания принадлежат оркестратору.

## Три метода

```python
from app.services.llm.contracts import GenerationContext
from app.services.llm.limits import StageBudget

context = GenerationContext.model_validate_json(context_json, strict=True)
# Каждый budget — актуальный остаток, зарезервированный оркестратором.
budget = StageBudget(remaining_seconds=120.0, remaining_api_calls=9,
                     remaining_generations=1, iteration=0)
scenarios = service.generate_scenarios(context, budget=budget)
# Учесть scenarios.metadata и передать обновлённый остаток следующему этапу.
bundle = service.generate_tests(context, scenarios, budget=next_budget)
# После проверки шлюзом/runner, с feedback для точного bundle_hash:
repair = service.repair_tests(context, scenarios, bundle, feedback, budget=repair_budget)
artifact_json = repair.model_dump_json()
```

`next_budget`, `repair_budget` и `feedback` предоставляет вызывающий оркестратор.
Сценарии/первичная генерация используют iteration=0; feedback и бюджет доработки — 1 или 2.
Каждый метод возвращает артефакт с `metadata`; при ошибке расход доступен в `error.metadata`.
Без budget действуют настройки одного этапа, без гарантии общего run-бюджета. Для рабочего
run оркестратор передаёт актуальные остатки, атомарно резервирует их и не вызывает повтор
того же этапа после подтверждённого результата. Сам фасад не хранит глобальные счётчики.

Count/generate/retry используют один deadline: минимум настройки этапа и остатка времени.
Каждый вызов порта count/generate списывает API-попытку, даже если он завершился ошибкой;
это консервативный учёт попыток, не биллинг провайдера. Retry генерации увеличивает
`generation_calls`, но не `generations`: один начатый этап даёт generations=1. Count не
считается генерацией. Ошибка до первого generate даёт generations=0. Нулевой остаток
секунд/попыток/генераций блокирует соответствующий вызов. SDK выполняет одну попытку;
повторы реализованы только в StageProvider. Транспортный timeout ограничивает текущий
синхронный запрос; поздний ответ отвергается, принудительной отмены потока здесь нет.

## Вход парсера

Ниже иллюстративный JSON контекста. `content_hash` — идентификатор подготовленного снимка,
его вычисляет владелец снимка, не модель.

```json
{
  "schema_version": "1",
  "run_id": "offline-example",
  "snapshot": {
    "kind": "upload",
    "snapshot_id": "offline-snapshot",
    "content_hash": "synthetic-double-v1"
  },
  "mode": "default",
  "sources": [
    {
      "source_id": "spec",
      "kind": "requirements",
      "text": "double(value) returns value * 2 for every integer."
    },
    {
      "source_id": "code",
      "kind": "code",
      "path": "sample.py",
      "text": "def double(value):\n    return value * 2\n"
    }
  ],
  "environment": {
    "profile_id": "python-pytest",
    "python_version": "3.13",
    "dependencies": [
      "pytest"
    ]
  },
  "generated_tests_dir": "generated_tests",
  "protected_paths": [
    "sample.py"
  ]
}
```

Для GitHub snapshot.kind=`github` нужны repository_id, source_sha/target_sha (40 lowercase
hex), snapshot_id/content_hash и источник kind=`diff`. Default требует description либо
requirements; upload требует requirements и default. Lazy выводит ожидания из diff/code
и маркируется `expectation_origin="inferred"`. Идентификаторы источников уникальны.
`optional=true` разрешён только вспомогательному code/existing_test: при переполнении
они исключаются одной группой, кроме ссылок текущих сценариев и measured changed_lines.
Обязательные данные, предыдущий bundle и feedback не обрезаются. Выполняется максимум
два логических count (каждый с ограниченными транспортными повторами).

Для покрытия E3/E4 передают `changed_lines: [{"path":"sample.py","source_id":"code",
"lines":[1,2]}]`: только исполняемые изменённые строки окончательного кандидата. `start_line`
источника (по умолчанию 1) задаёт смещение фрагмента; строки должны входить в него. Пока
поддерживается один измеряемый фрагмент на путь. Связь со снимком проверяется по ID/хешам;
семантическую достоверность контекста обеспечивает парсер, не DTO-валидатор.

## Сериализованные артефакты

Иллюстративные DTO без необязательного metadata; результаты фасада заполняют его сами.
Модель возвращает только draft: run/snapshot, prompt_version, hash и metadata присваивает сервис.

```json
{"scenarios":[{"scenario_id":"double-integers","given":["An integer value"],"when":["double(value) is called"],"then":["The result equals value * 2"],"source_ids":["spec","code"]}],"ambiguities":[],"schema_version":"1","run_id":"offline-example","snapshot_id":"offline-snapshot","expectation_origin":"requirements","prompt_version":"scenarios.default.v1","omitted_source_ids":[]}
```

```json
{"files":[{"path":"generated_tests/test_sample.py","content":"from sample import double\n\ndef test_double():\n    assert double(3) == 6\n    assert double(0) == 0\n    assert double(-2) == -4\n","scenario_ids":["double-integers"]}],"schema_version":"1","run_id":"offline-example","snapshot_id":"offline-snapshot","bundle_hash":"b19676521bec1d2154b436e762505e45514168168fdade7d0c9cc25f95e2d948","prompt_version":"tests.pytest.v1","omitted_source_ids":[]}
```

Bundle hash — SHA-256 канонического JSON точных path/content/scenario_ids, независимый от
порядка файлов/связей. Он проверяется при десериализации. Всякий сценарий должен быть связан
с файлом; пути относительные POSIX, `.py`, только под generated_tests_dir и вне protected_paths.
Это структурная граница; содержимое Python дополнительно проверяют шлюз и sandbox.

## Feedback и доработка

`ValidationFeedback` содержит schema_version, run_id, snapshot_id, bundle_hash, iteration,
reason, issues и необязательный coverage. Пример feedback для bundle выше:

```json
{"schema_version":"1","run_id":"offline-example","snapshot_id":"offline-snapshot","bundle_hash":"b19676521bec1d2154b436e762505e45514168168fdade7d0c9cc25f95e2d948","iteration":1,"reason":"collection_error","issues":[{"kind":"import","message":"Module import failed","path":"generated_tests/test_sample.py","line":1}]}
```

Это пример структуры, а не утверждение о результате проверки. Допустимые причины:
validation_error, collection_error, test_failure, coverage_gap, coverage_missing.
Тип issue: syntax/import/assertion/existing_test/environment/static; line требует path.
Для coverage_gap нужен coverage с executable_changed_lines, covered_changed_lines и
uncovered=[{path, lines}]. Сумма покрытых и непокрытых равна числу измеренных строк;
пути/строки сверяются с context.changed_lines. Coverage отсутствует — неизвестно, не 0%.
coverage_missing отвергается до API: сначала требуется корректный отчёт внешнего runner.

RepairResult возвращает action=`replace` с полным новым bundle либо `cannot_repair`
с explanation без bundle. Сохраняются previous_bundle_hash, iteration и все прежние
связи файл→сценарий; удаление/переименование файлов и неизменившийся bundle отклоняются.
Даже replace требует новой внешней валидации и нового feedback. Cannot_repair не означает
успех. Production/existing-test/environment-проблемы нельзя исправлять ослаблением тестов.

## Ошибки и наблюдаемость

```python
from app.services.llm.provider import ProviderError
try:
    bundle = service.generate_tests(context, scenarios, budget=next_budget)
except ProviderError as error:
    code = error.code
    accounting = error.metadata  # Также при невалидном входе; расход может быть нулевым.
```

Основные коды: invalid_context/scenarios/feedback/budget/iteration; context_limit_exceeded;
deadline_exceeded, api_budget_exhausted, generation_budget_exhausted, repair_limit_exceeded;
provider_unauthorized/forbidden/quota/timeout/unavailable, attempts_exhausted;
provider_refusal, truncated_response, incomplete_response, empty_response, invalid_json/schema;
invalid_source_reference, invalid_test_bundle/repair_bundle, repair_unchanged,
coverage_report_missing. Непредвиденная диагностика порта заменяется на provider_failure.
Невалидные настройки дают ConfigurationError без исходного ввода. Никакая ошибка не
возвращает пустой успешный набор; автоматический повтор целого этапа фасад не делает.

Metadata связывает stage/backend/model_id/prompt_version с run_id/snapshot_id/iteration,
содержит duration_seconds, api_calls/count_calls/generation_calls/generations, usage,
error_code, last_provider_error_code и bundle_hash. Последний безопасный код
провайдера сохраняется и при attempts_exhausted; неизвестная диагностика скрывается. Usage относится только к доступному ответу генерации;
неизвестные поля остаются None, известный 0 сохраняется. Это не суммарная стоимость run.
Журналировать разрешается только `metadata.log_fields()`: фиксированные stage/backend и
числовые счётчики/время. Остальные поля и полные DTO — только в защищённом хранилище
артефактов с доступом к своему run; repr скрыт. Не журналируйте model_dump, JSON, SDK
exceptions или ValidationError.errors(): они могут содержать пользовательские данные.

## CLI и оставшаяся интеграция

```sh
PYTHONPATH=backend python -m app.services.llm.demo --offline
PYTHONPATH=backend python -m app.services.llm.demo --live --input context.json
PYTHONPATH=backend python -m app.services.llm.demo --live --input context.json --scenarios scenarios.json --bundle bundle.json --feedback feedback.json
```

Offline — встроенный scripted fake без ключа/SDK/сети; live требует ключ и модель в окружении.
CLI ограничивает каждый JSON-файл 2 MB, ничего не записывает в репозиторий и не запускает
полученный Python. Exit 0 означает получение непроверенного артефакта; 1 — ошибку входа,
конфигурации или этапа; 2 — неверные флаги. При ошибке второго этапа stdout сохраняет
сценарии вместе с error/metadata. При ошибке первого этапа/repair JSON-ошибка идёт в stderr.
Полный stdout содержит артефакты: направляйте его в защищённое хранилище, а не общий лог.

Подключение парсера, оркестратора, шлюза и sandbox runner остаётся внешней работой.
Качество, фактическое покрытие и обнаружение намеренной ошибки требуют реального runner;
генерация, fake-ответ и синтетический feedback этого не доказывают. Порог ≥90%, нулевой
знаменатель, актуальность кандидата и допуск к публикации/мержу проверяет внешний контур.
