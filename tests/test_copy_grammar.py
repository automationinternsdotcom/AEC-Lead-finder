from scout.copy_grammar import review_copy


def test_missing_article_and_greeting_are_repaired_without_rewriting():
    before = "Hi Chris just wanted to reach out since I saw that security building is being renovated into 255-room hotel. Is there any chance you'll be reviewing your janitorial needs?"
    after = review_copy(before, ("Security Building",))
    assert after == "Hi Chris, I just wanted to reach out since I saw that Security Building is being renovated into a 255-room hotel. Is there any chance you'll be reviewing your janitorial needs?"
    assert review_copy(after, ("Security Building",)) == after


def test_plural_mass_nouns_and_protected_content_unchanged():
    for text in ("converted into apartments", "renovated into affordable housing", "into a 255-room hotel", "into an office", "{{custom.projectPropertyName}}", '<a href="https://example.com/into-hotel">Unsubscribe</a>'):
        assert review_copy(text) == text


def test_articles_and_reference_case():
    assert review_copy("renovated into 8-room hotel") == "renovated into an 8-room hotel"
    assert review_copy("plans for industrial park in Phoenix.") == "plans for an industrial park in Phoenix."
    assert review_copy("renovated into entertainment and dining district") == "renovated into an entertainment and dining district"
    assert review_copy("received commission recommendation") == "received a commission recommendation"
    assert review_copy("janitorial needs for Security Building?") == "janitorial needs for Security Building?"
