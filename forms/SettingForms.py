from flask_wtf import FlaskForm
from wtforms import StringField, IntegerField, TextAreaField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Optional
from wtforms.widgets import PasswordInput


class SettingsForm(FlaskForm):
    host = StringField("Host", validators=[DataRequired()])
    port = IntegerField("Port", validators=[DataRequired()], default=1883)
    login = StringField("Login", validators=[Optional()])
    password = StringField(
        "Password",
        validators=[Optional()],
        widget=PasswordInput(hide_value=False),
    )
    topic_prefix = StringField("Topic Prefix", validators=[DataRequired()], default="home1")
    whitelist_classes = TextAreaField("Whitelist Classes", validators=[Optional()])
    whitelist_objects = TextAreaField("Whitelist Objects", validators=[Optional()])
    blacklist_classes = TextAreaField("Blacklist Classes", validators=[Optional()])
    blacklist_objects = TextAreaField("Blacklist Objects", validators=[Optional()])
    enable_inbound = BooleanField("Enable inbound sync", validators=[Optional()], default=True)
    submit = SubmitField("Submit")
