from arm.ui import db


class Film(db.Model):
    """
    One film of a disc that carries several (a double feature, a "1 & 2" collection).

    A job keeps one title, year and output folder; that is the first film on the disc. Each
    further film gets its own row here, its own ``Title (Year)`` folder under the movies path,
    and the ripped title whose runtime matches it (see ``arm_ripper.file_collection``).
    """
    film_id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('job.job_id'))
    position = db.Column(db.Integer)
    """1-based order on the disc / in the product title; position 1 is the job's own title"""
    title = db.Column(db.String(256))
    """Title cleaned for use in a filename, like job.title"""
    year = db.Column(db.String(4))
    imdb_id = db.Column(db.String(15))
    video_type = db.Column(db.String(20))
    poster_url = db.Column(db.String(256))
    runtime = db.Column(db.Integer)
    """Runtime the metadata provider gives, in seconds; None when unknown"""
    track_number = db.Column(db.String(4))
    """track_number of the ripped title filed as this film, once the rip has been filed"""

    def __init__(self, job_id, position, title, year=None, imdb_id=None, video_type="movie",
                 poster_url=None, runtime=None):
        self.job_id = job_id
        self.position = position
        self.title = title
        self.year = year
        self.imdb_id = imdb_id
        self.video_type = video_type
        self.poster_url = poster_url
        self.runtime = runtime
        self.track_number = None

    @property
    def folder_name(self):
        """``Title (Year)``, or just the title when the year is unknown; the same shape as the job's folder"""
        if self.year and self.year not in ("0000", ""):
            return f"{self.title} ({self.year})"
        return str(self.title)

    def __repr__(self):
        return f'<Film {self.position}: {self.title}>'

    def get_d(self):
        """Return a dict of the object, without the SQLAlchemy state"""
        return {str(key): str(value) for key, value in self.__dict__.items() if '_sa_instance_state' not in key}
