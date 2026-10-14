from jobspy.model import CompensationInterval, JobType, Compensation
from jobspy.util import get_enum_from_job_type


def get_job_type(attributes: list) -> list[JobType]:
    """
    Parses the attributes to get list of job types
    :param attributes:
    :return: list of JobType
    """
    job_types: list[JobType] = []
    for attribute in attributes:
        job_type_str = attribute.get("label", "").replace("-", "").replace(" ", "").lower()
        job_type = get_enum_from_job_type(job_type_str)
        if job_type:
            job_types.append(job_type)
    return job_types


def get_compensation(compensation: dict) -> Compensation | None:
    """
    Parses the job to get compensation
    :param compensation:
    :return: compensation object
    """
    if not compensation or (
        not compensation.get("baseSalary") and not compensation.get("estimated")
    ):
        return None
    comp = compensation.get("baseSalary") or compensation["estimated"].get(
        "baseSalary"
    )
    if not comp:
        return None
    interval = get_compensation_interval(comp["unitOfWork"])
    if not interval:
        return None
    min_range = comp["range"].get("min")
    max_range = comp["range"].get("max")
    return Compensation(
        interval=interval,
        min_amount=int(min_range) if min_range is not None else None,
        max_amount=int(max_range) if max_range is not None else None,
        currency=(
            compensation["estimated"]["currencyCode"]
            if compensation["estimated"]
            else compensation["currencyCode"]
        ),
    )


REMOTE_KEYWORDS = ("remote", "work from home", "wfh")


def _mentions_remote(text: str | None) -> bool:
    return any(keyword in (text or "").lower() for keyword in REMOTE_KEYWORDS)


def is_job_remote(job: dict, description: str | None) -> bool:
    """
    Searches the description, location, and attributes to check if job is remote
    """
    long_location = ((job.get("location") or {}).get("formatted") or {}).get("long")
    return (
        any(_mentions_remote(attr.get("label")) for attr in job.get("attributes") or [])
        or _mentions_remote(description)
        or _mentions_remote(long_location)
    )
