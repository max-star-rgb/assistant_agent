from assistant_agent.schemas.personal_assistant import (
    CalendarCreateRequest,
    CalendarSearchRequest,
    ContactsSearchRequest,
    ReminderCreateRequest,
    WeatherRequest,
)
from assistant_agent.services.personal_assistant_adapters import (
    MockCalendarAdapter,
    MockContactsAdapter,
    MockReminderAdapter,
    MockWeatherAdapter,
    UnconfiguredCalendarAdapter,
    UnconfiguredContactsAdapter,
    UnconfiguredReminderAdapter,
    UnconfiguredWeatherAdapter,
)


def test_mock_personal_assistant_adapters_return_stable_offline_results() -> None:
    weather = MockWeatherAdapter().lookup(WeatherRequest(location="Shanghai"))
    calendar = MockCalendarAdapter().search(CalendarSearchRequest(query="today"))
    contacts = MockContactsAdapter().search(ContactsSearchRequest(query="alex"))
    reminder = MockReminderAdapter().create(
        ReminderCreateRequest(title="Call customer", idempotency_key="reminder-1")
    )

    assert weather.success is True
    assert weather.provider == "mock"
    assert weather.forecast[0].condition == "clear"
    assert weather.output_ref == "mock://weather/shanghai"
    assert calendar.success is True
    assert calendar.events[0].title == "Product sync"
    assert calendar.raw_data_ref == "mock://calendar/events/today"
    assert contacts.success is True
    assert contacts.contacts[0].display_name == "Alex Chen"
    assert contacts.raw_data_ref == "mock://contacts/alex"
    assert reminder.success is True
    assert reminder.output_ref == "mock://reminders/reminder-1"


def test_unconfigured_personal_assistant_adapters_do_not_fallback_to_mock_or_leak_keys() -> None:
    results = [
        UnconfiguredWeatherAdapter("google", "GOOGLE_WEATHER_API_KEY").lookup(
            WeatherRequest(location="Shanghai")
        ),
        UnconfiguredCalendarAdapter("google", "GOOGLE_CALENDAR_API_KEY").search(
            CalendarSearchRequest(query="today")
        ),
        UnconfiguredContactsAdapter("google", "GOOGLE_CONTACTS_API_KEY").search(
            ContactsSearchRequest(query="alex")
        ),
        UnconfiguredReminderAdapter("google", "GOOGLE_REMINDER_API_KEY").create(
            ReminderCreateRequest(title="Call customer", idempotency_key="reminder-1")
        ),
    ]

    for result in results:
        rendered = str(result.model_dump(mode="json"))

        assert result.success is False
        assert result.provider == "google"
        assert result.errors[0]["code"] == "provider_unconfigured"
        assert "sk-" not in rendered
        assert "Bearer" not in rendered
        assert not result.output_ref.startswith("mock://")
