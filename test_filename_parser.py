from datetime import date

from filename_parser import kind_from_name, parse_filename, parse_year_folder


def check(name, d, mtype, review=False, kind="packet"):
    p = parse_filename(name, default_kind="packet")
    assert p.meeting_date == d, p
    assert p.meeting_type == mtype, p
    assert p.needs_review is review, p
    assert p.doc_kind == kind, p


def test_current_archive_names():
    check("2025-10-07 City Council Meeting", date(2025, 10, 7), "regular")
    check("2025-10-07 Work Session Meeting", date(2025, 10, 7), "work_session")
    check("2025-11-12 Special Work Session Meeting", date(2025, 11, 12), "special_work_session")


def test_names_seen_in_older_files():
    check("2018-01-03 City Council Regular Meeting Packet.pdf", date(2018, 1, 3), "regular")
    check("2014-10-07_COUNCIL MEETING", date(2014, 10, 7), "regular")
    check("2017 02 22%20City%20Council%20Regular%20Meeting%20Packet", date(2017, 2, 22), "regular")
    check("2015 11 17 City Council Meetng", date(2015, 11, 17), "regular")  # city's typo


def test_minutes_kind_comes_from_name():
    check("2019-03-05 City Council Minutes", date(2019, 3, 5), "regular", kind="minutes")


def test_unparseable_names_are_flagged_not_raised():
    for bad in ["Index of Resolutions", "", "2025-13-45 City Council Meeting", "2025-10-07 Budget Binder"]:
        assert parse_filename(bad).needs_review, bad


def test_year_folders():
    assert parse_year_folder("2025") == (2025, 2025)
    assert parse_year_folder("1974-1979") == (1974, 1979)
    assert parse_year_folder("1981-1985 Agendas") == (1981, 1985)
    assert parse_year_folder("1982-1999") == (1982, 1999)
    assert parse_year_folder("Meeting Packets_Archive") is None


def test_folder_kind():
    assert kind_from_name("Meeting Packets_Archive") == "packet"
    assert kind_from_name("Minutes_Archive") == "minutes"
    assert kind_from_name("1981-1985 Agendas") == "agenda"
    assert kind_from_name("2025") is None