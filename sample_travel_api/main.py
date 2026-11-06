
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
from uuid import uuid4

app = FastAPI(
    title="Mock B2B Travel API",
    description="Simulated B2B travel booking platform for InvariantDAST testing",
    version="1.0.0"
)

# -----------------------------------
# IN-MEMORY MOCK DATABASE
# -----------------------------------

flights = {
    "FL001": {
        "flight_id": "FL001",
        "airline": "Air India",
        "origin": "CJB",
        "destination": "DEL",
        "price": 5500,
        "available_seats": 20
    },
    "FL002": {
        "flight_id": "FL002",
        "airline": "IndiGo",
        "origin": "CJB",
        "destination": "BLR",
        "price": 3200,
        "available_seats": 15
    }
}

quotes = {}
bookings = {}


# -----------------------------------
# REQUEST MODELS
# -----------------------------------

class FlightSearch(BaseModel):
    origin: str
    destination: str


class QuoteRequest(BaseModel):
    flight_id: str
    passengers: int = Field(gt=0, le=9)


class HoldRequest(BaseModel):
    quote_id: str


class AmendmentRequest(BaseModel):
    passenger_name: str = Field(min_length=2, max_length=100)


# -----------------------------------
# HEALTH CHECK
# -----------------------------------

@app.get("/")
def home():
    return {
        "application": "Mock B2B Travel API",
        "status": "running"
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


# -----------------------------------
# 1. FLIGHT SEARCH
# -----------------------------------

@app.post("/flights/search")
def search_flights(request: FlightSearch):

    results = [
        flight for flight in flights.values()
        if flight["origin"] == request.origin.upper()
        and flight["destination"] == request.destination.upper()
        and flight["available_seats"] > 0
    ]

    return {
        "flights": results,
        "count": len(results)
    }


# -----------------------------------
# 2. FARE QUOTE
# -----------------------------------

@app.post("/quotes")
def create_quote(
    request: QuoteRequest,
    agency_id: str = Header(..., alias="X-Agency-ID")
):

    flight = flights.get(request.flight_id)

    if not flight:
        raise HTTPException(404, "Flight not found")

    if flight["available_seats"] < request.passengers:
        raise HTTPException(400, "Insufficient seats")

    quote_id = str(uuid4())

    total_price = flight["price"] * request.passengers

    quotes[quote_id] = {
        "quote_id": quote_id,
        "flight_id": request.flight_id,
        "agency_id": agency_id,
        "passengers": request.passengers,
        "total_price": total_price,
        "currency": "INR",
        "status": "ACTIVE"
    }

    return quotes[quote_id]


# -----------------------------------
# 3. BOOKING HOLD
# -----------------------------------

@app.post("/bookings/hold")
def create_booking(
    request: HoldRequest,
    agency_id: str = Header(..., alias="X-Agency-ID")
):

    quote = quotes.get(request.quote_id)

    if not quote:
        raise HTTPException(404, "Quote not found")

    if quote["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized quote access")

    if quote["status"] != "ACTIVE":
        raise HTTPException(400, "Quote is not active")

    flight = flights[quote["flight_id"]]

    if flight["available_seats"] < quote["passengers"]:
        raise HTTPException(400, "Insufficient seats")

    flight["available_seats"] -= quote["passengers"]

    booking_id = str(uuid4())

    bookings[booking_id] = {
        "booking_id": booking_id,
        "agency_id": agency_id,
        "flight_id": quote["flight_id"],
        "passengers": quote["passengers"],
        "total_price": quote["total_price"],
        "passenger_name": "Demo Passenger",
        "status": "HELD"
    }

    quote["status"] = "USED"

    return bookings[booking_id]


# -----------------------------------
# 4. TICKET ISSUANCE
# -----------------------------------

@app.post("/bookings/{booking_id}/issue")
def issue_ticket(
    booking_id: str,
    agency_id: str = Header(..., alias="X-Agency-ID")
):

    booking = bookings.get(booking_id)

    if not booking:
        raise HTTPException(404, "Booking not found")

    if booking["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized booking access")

    if booking["status"] != "HELD":
        raise HTTPException(400, "Booking must be HELD")

    booking["status"] = "ISSUED"
    booking["ticket_id"] = "TKT-" + uuid4().hex[:8].upper()

    return {
        "message": "Ticket issued successfully",
        "booking": booking
    }


# -----------------------------------
# 5. BOOKING AMENDMENT
# -----------------------------------

@app.patch("/bookings/{booking_id}/amend")
def amend_booking(
    booking_id: str,
    request: AmendmentRequest,
    agency_id: str = Header(..., alias="X-Agency-ID")
):

    booking = bookings.get(booking_id)

    if not booking:
        raise HTTPException(404, "Booking not found")

    if booking["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized booking access")

    if booking["status"] != "ISSUED":
        raise HTTPException(400, "Only issued bookings can be amended")

    booking["passenger_name"] = request.passenger_name

    return {
        "message": "Booking amended successfully",
        "booking": booking
    }


# -----------------------------------
# 6. REFUND PROCESSING
# -----------------------------------

@app.post("/bookings/{booking_id}/refund")
def refund_booking(
    booking_id: str,
    agency_id: str = Header(..., alias="X-Agency-ID")
):

    booking = bookings.get(booking_id)

    if not booking:
        raise HTTPException(404, "Booking not found")

    if booking["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized booking access")

    if booking["status"] != "ISSUED":
        raise HTTPException(400, "Only issued bookings can be refunded")

    booking["status"] = "REFUNDED"

    flight = flights[booking["flight_id"]]
    flight["available_seats"] += booking["passengers"]

    return {
        "message": "Refund processed successfully",
        "refund_amount": booking["total_price"],
        "booking": booking
    }


# -----------------------------------
# VIEW BOOKING
# -----------------------------------

@app.get("/bookings/{booking_id}")
def get_booking(
    booking_id: str,
    agency_id: str = Header(..., alias="X-Agency-ID")
):

    booking = bookings.get(booking_id)

    if not booking:
        raise HTTPException(404, "Booking not found")

    if booking["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized booking access")

    return booking
