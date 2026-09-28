from app.mail.base import unwrap_forward


def test_outlook_forward_is_unwrapped():
    body = """

________________________________
From: Moodle <noreply@moodle.iiit.ac.in>
Sent: Monday, September 28, 2026 10:02 AM
To: Rudra Choudhary <rudra.choudhary@research.iiit.ac.in>
Subject: CS3.301 Assignment 3 released

Dear student,
Assignment 3 is due Friday 11:59 PM."""
    sender, subject, text = unwrap_forward("Rudra <rudra.choudhary@research.iiit.ac.in>", "FW: CS3.301 Assignment 3 released", body)
    assert sender == "Moodle <noreply@moodle.iiit.ac.in>"
    assert subject == "CS3.301 Assignment 3 released"
    assert text.startswith("Dear student") and "Sent:" not in text


def test_gmail_style_forward():
    body = "---------- Forwarded message ---------\nFrom: Robotics Club <robotics@students.iiit.ac.in>\nDate: Mon, 28 Sep 2026\nSubject: Hackathon\nTo: <me@x>\n\nRegister now!"
    sender, subject, text = unwrap_forward("me@x", "Fwd: Hackathon", body)
    assert sender.startswith("Robotics Club") and subject == "Hackathon" and text == "Register now!"


def test_non_forward_untouched():
    assert unwrap_forward("a@b", "Hello", "From: someone\nbody") == ("a@b", "Hello", "From: someone\nbody")
