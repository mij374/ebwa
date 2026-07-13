"""
Seed realistic EBWA demo content into a fresh database.

Usage:
    python seed_demo.py

Creates events, news posts, testimonials, partners and fills the
home/about content blocks. Images are left blank — upload real photos
via the admin. Refuses to run if any events already exist.
"""
import sys
from datetime import date, timedelta

from app import (app, db, DEFAULT_BLOCKS, Block, Event, NewsPost,
                 Partner, Testimonial, unique_slug)

TODAY = date.today()


BLOCK_VALUES = {
    "home_hero_title": "Standing with our community, every step of the way.",
    "home_hero_text": (
        "For over two decades EBWA has worked to relieve economic deprivation "
        "and social isolation among the Bangladeshi and wider communities of "
        "Enfield — through education, welfare support, training and the simple "
        "power of bringing people together."),
    "home_stat_1": "100+",
    "home_stat_2": "24",
    "home_stat_3": "25",
    "home_stat_4": "20",
    "about_title": "About EBWA",
    "about_body": (
        "The Enfield Bangladesh Welfare Association was founded to relieve the "
        "economic deprivation and social isolation experienced by Bangladeshi "
        "families in Enfield, and our door has been open to the whole community "
        "ever since.\n\n"
        "From our centre on Ponders End High Street we run weekend Arabic and "
        "Bengali schools, a weekly elderly drop-in, employability and childcare "
        "courses for women, free advice and translation services, and health "
        "and wellbeing programmes for every generation.\n\n"
        "We believe nobody in Enfield should face hardship alone. Whether you "
        "need support, want to learn, or simply fancy a cup of tea and good "
        "company — you are welcome here."),
}


EVENTS = [
    # (days from today, title, start_time, venue, summary, description)
    (5, "Elderly Drop-in & Fitness Session", "11:00 AM",
     "EBWA Centre, 180 High Street, Ponders End",
     "Weekly recreation, gentle exercise and lunch for our older members.",
     "Join us for our regular drop-in session for older residents.\n\n"
     "Gentle chair-based exercise, board games, a hot lunch and plenty of "
     "conversation — a friendly antidote to a quiet week. New faces are "
     "always welcome, and no booking is needed."),
    (19, "Summer Seaside Trip to Southend", "9:00 AM",
     "Coach departs from the EBWA Centre",
     "Our annual family day out — coach, beach and fish and chips.",
     "Our much-loved annual seaside outing returns.\n\n"
     "The coach leaves Ponders End at 9am sharp and returns by early "
     "evening. Bring the whole family — there will be games on the beach, "
     "ice cream for the children and time to explore the pier.\n\n"
     "Places are limited, so please call the centre to reserve seats."),
    (40, "Eid in the Park", "12:00 PM",
     "Ponders End Recreation Ground",
     "Food, funfair stalls and festivities for the whole of Enfield.",
     "Everyone is invited to our Eid celebration in the park.\n\n"
     "Expect delicious food, henna and craft stalls, children's rides and "
     "performances from our weekend school pupils. Eid in the Park has "
     "grown into one of Enfield's warmest community gatherings — bring "
     "your neighbours, whatever their background."),
    (61, "Annual General Meeting 2026", "6:30 PM",
     "EBWA Centre, 180 High Street, Ponders End",
     "Hear what we achieved this year and help shape the year ahead.",
     "All members and friends of EBWA are warmly invited to our AGM.\n\n"
     "We will review the past year's programmes and accounts, elect the "
     "management committee and share our plans for the year ahead. Light "
     "refreshments will be served."),
    (-9, "Elderly Drop-in & Health Check Morning", "11:00 AM",
     "EBWA Centre, 180 High Street, Ponders End",
     "Blood pressure checks and health advice alongside the usual drop-in.",
     "A special edition of our weekly drop-in, with local health visitors "
     "offering blood pressure checks and friendly advice.\n\n"
     "Thank you to everyone who came along and to the NHS team who joined "
     "us for the morning."),
    (-32, "Spring Seaside Trip to Clacton", "9:00 AM",
     "Coach departed from the EBWA Centre",
     "A full coach and glorious sunshine for our spring outing.",
     "Fifty of us enjoyed a wonderful day out in Clacton-on-Sea.\n\n"
     "The weather was kind, the fish and chips were plentiful and the "
     "coach rang with songs all the way home. Thank you to our volunteers "
     "who made the day run so smoothly."),
    (-75, "Community Iftar Evening", "8:00 PM",
     "EBWA Centre, 180 High Street, Ponders End",
     "Neighbours of all faiths broke bread together at our open iftar.",
     "Our open iftar brought together neighbours of every faith and none "
     "to share a meal as the sun set.\n\n"
     "Councillors, police colleagues and friends from local churches "
     "joined Bangladeshi families for an evening that summed up "
     "everything EBWA stands for."),
]


NEWS = [
    # (days ago, title, summary, body)
    (4, "New term begins at our weekend Bengali and Arabic schools",
     "Registration is open for the autumn term — all children welcome.",
     "Our weekend supplementary schools return for the new term this "
     "month, offering Bengali language, Arabic and cultural activities "
     "for children across Enfield.\n\n"
     "The schools help children stay connected to their heritage while "
     "building confidence that carries into their weekday classrooms. "
     "Fees are kept as low as possible and no child is ever turned away "
     "over money.\n\n"
     "To register, drop into the centre on Ponders End High Street or "
     "call us — places fill quickly."),
    (16, "Twenty-four women graduate from our childcare course",
     "Congratulations to our largest ever childcare and employability cohort.",
     "We are bursting with pride: twenty-four local women have completed "
     "our accredited childcare course, many of them returning to learning "
     "for the first time in years.\n\n"
     "The course combines childcare qualifications with employability "
     "skills, CV support and confidence building. Several graduates have "
     "already secured work placements with local nurseries.\n\n"
     "A warm thank you to our tutors and volunteers, and to every "
     "graduate — your determination inspires the whole community."),
    (31, "Cricket project gives 25 young people a sporting start",
     "Our youth cricket sessions are building fitness, friendship and focus.",
     "Twenty-five young people are now training regularly through our "
     "community cricket project, developing fitness, discipline and "
     "friendships that reach well beyond the boundary rope.\n\n"
     "The project keeps young people active and engaged during evenings "
     "and weekends, with qualified coaches and equipment provided free "
     "of charge.\n\n"
     "We are grateful to our partners for the use of local grounds, and "
     "we are already planning friendly matches against neighbouring "
     "clubs later in the season."),
    (48, "Twenty residents trained in life-saving first aid",
     "Free first aid training is building a safer, more confident community.",
     "Twenty members of our community completed certified first aid "
     "training at the centre this month, learning CPR, how to help "
     "someone who is choking and how to respond in an emergency.\n\n"
     "The free sessions are part of our health and wellbeing programme, "
     "which aims to make sure that in every street in our community "
     "there is someone with the skills to help.\n\n"
     "More dates will be announced soon — call the centre to register "
     "your interest."),
]


TESTIMONIALS = [
    ("Rahima Begum", "Parent, Bengali school",
     "My daughters look forward to Saturday mornings all week. They are "
     "learning the language of their grandparents and making friends for "
     "life — I could not ask for more."),
    ("Abdul Karim", "Elderly drop-in member",
     "After my wife passed away the flat felt very quiet. The drop-in gave "
     "me a reason to get up on a Tuesday. Now I have friends, hot food and "
     "plenty of laughter every week."),
    ("Nasrin Sultana", "Childcare course graduate",
     "I hadn't studied since leaving school in Sylhet. EBWA believed in me, "
     "and now I have a qualification and a job interview at a local "
     "nursery. My children are so proud of their mum."),
    ("Mohammed Shahid Miah", "Volunteer",
     "I started by helping to set out chairs at the drop-in. Volunteering "
     "with EBWA has given me skills, references and the feeling that I am "
     "putting something back into Enfield."),
    ("Fatema Khatun", "Advice service user",
     "When the letters from the council stopped making sense, EBWA sat "
     "with me, translated everything and sorted it out. They treat you "
     "like family here."),
]


PARTNERS = [
    ("Enfield Council", "https://www.enfield.gov.uk",
     "Working together on community programmes across the borough."),
    ("Enfield Voluntary Action", "",
     "Supporting and connecting Enfield's voluntary sector."),
    ("Citizens Advice Enfield", "",
     "Joint advice sessions on benefits, housing and debt."),
    ("Age UK Enfield", "",
     "Partnering on activities that tackle isolation among older people."),
    ("North Enfield Foodbank", "",
     "Referrals and collections for families facing hardship."),
    ("Metropolitan Police — Enfield", "",
     "Community safety, legal awareness and crime prevention work."),
]


def seed():
    db.create_all()

    if Event.query.count():
        print("Refusing to seed: the database already contains "
              "%d event(s). Use a fresh database." % Event.query.count())
        sys.exit(1)

    # Content blocks: insert any missing defaults, then fill demo copy.
    for group, key, label, kind, value in DEFAULT_BLOCKS:
        if not Block.query.filter_by(key=key).first():
            db.session.add(Block(group=group, key=key, label=label,
                                 kind=kind, value=value))
    db.session.flush()
    blocks_filled = 0
    for key, value in BLOCK_VALUES.items():
        b = Block.query.filter_by(key=key).first()
        if b:
            b.value = value
            blocks_filled += 1

    for days, title, start_time, venue, summary, description in EVENTS:
        ev = Event()
        ev.title = title
        ev.slug = unique_slug(Event, title)
        ev.event_date = TODAY + timedelta(days=days)
        ev.start_time = start_time
        ev.venue = venue
        ev.summary = summary
        ev.description = description
        ev.published = True
        db.session.add(ev)

    for days_ago, title, summary, body in NEWS:
        post = NewsPost()
        post.title = title
        post.slug = unique_slug(NewsPost, title)
        post.published_date = TODAY - timedelta(days=days_ago)
        post.summary = summary
        post.body = body
        post.published = True
        db.session.add(post)

    for i, (name, role, quote) in enumerate(TESTIMONIALS):
        db.session.add(Testimonial(name=name, role=role, quote=quote,
                                   published=True, sort=i))

    for i, (name, url, blurb) in enumerate(PARTNERS):
        db.session.add(Partner(name=name, url=url, blurb=blurb, sort=i))

    db.session.commit()

    upcoming = sum(1 for e in EVENTS if e[0] >= 0)
    print("Demo content created:")
    print("  %d events (%d upcoming, %d past)"
          % (len(EVENTS), upcoming, len(EVENTS) - upcoming))
    print("  %d news posts" % len(NEWS))
    print("  %d testimonials" % len(TESTIMONIALS))
    print("  %d partners" % len(PARTNERS))
    print("  %d content blocks filled" % blocks_filled)
    print("Images were left blank - upload photos via the admin.")


if __name__ == "__main__":
    with app.app_context():
        seed()
