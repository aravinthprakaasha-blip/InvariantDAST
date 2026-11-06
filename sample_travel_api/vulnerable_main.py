"""DELIBERATELY VULNERABLE copy of the mock B2B travel API.

Used to demonstrate and regression-test InvariantDAST findings. Never deploy this.
Planted bugs (each one maps to a scanner check):
  1. GET /bookings/{id}           no tenant ownership check            -> bola_read, unauth_read
  2. POST /bookings/hold          trusts client-supplied total_price   -> fare_tamper
  3. POST /bookings/hold          quote is never marked USED           -> quote_replay
  4. POST /bookings/{id}/refund   allowed from HELD, not just ISSUED   -> skip_refund
  5. POST /bookings/{id}/refund   check-then-act race window           -> refund_race
Run:  uvicorn sample_travel_api.vulnerable_main:app --port 8003
"""

import time
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="Vulnerable B2B Travel API (demo)", version="0.0.1-vulnerable")

flights = {
    "FL001": {"flight_id": "FL001", "airline": "Air India", "origin": "CJB", "destination": "DEL", "price": 5500, "available_seats": 20},
    "FL002": {"flight_id": "FL002", "airline": "IndiGo", "origin": "CJB", "destination": "BLR", "price": 3200, "available_seats": 15},
}
quotes: dict = {}
bookings: dict = {}


class FlightSearch(BaseModel):
    origin: str
    destination: str


class QuoteRequest(BaseModel):
    flight_id: str
    passengers: int = Field(gt=0, le=9)


class HoldRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    quote_id: str
    total_price: float | None = None   # BUG 2: client can override the price


class AmendmentRequest(BaseModel):
    passenger_name: str = Field(min_length=2, max_length=100)


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/flights/search")
def search_flights(request: FlightSearch):
    results = [f for f in flights.values()
               if f["origin"] == request.origin.upper() and f["destination"] == request.destination.upper()
               and f["available_seats"] > 0]
    return {"flights": results, "count": len(results)}


@app.post("/quotes")
def create_quote(request: QuoteRequest, agency_id: str = Header(..., alias="X-Agency-ID")):
    flight = flights.get(request.flight_id)
    if not flight:
        raise HTTPException(404, "Flight not found")
    if flight["available_seats"] < request.passengers:
        raise HTTPException(400, "Insufficient seats")
    quote_id = str(uuid4())
    quotes[quote_id] = {"quote_id": quote_id, "flight_id": request.flight_id, "agency_id": agency_id,
                        "passengers": request.passengers, "total_price": flight["price"] * request.passengers,
                        "currency": "INR", "status": "ACTIVE"}
    return quotes[quote_id]


@app.post("/bookings/hold")
def create_booking(request: HoldRequest, agency_id: str = Header(..., alias="X-Agency-ID")):
    quote = quotes.get(request.quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized quote access")
    flight = flights[quote["flight_id"]]
    if flight["available_seats"] < quote["passengers"]:
        raise HTTPException(400, "Insufficient seats")
    flight["available_seats"] -= quote["passengers"]
    booking_id = str(uuid4())
    bookings[booking_id] = {
        "booking_id": booking_id, "agency_id": agency_id, "flight_id": quote["flight_id"],
        "passengers": quote["passengers"],
        "total_price": request.total_price if request.total_price is not None else quote["total_price"],  # BUG 2
        "passenger_name": "Demo Passenger", "status": "HELD",
    }
    # BUG 3: quote["status"] is never set to "USED"
    return bookings[booking_id]


@app.post("/bookings/{booking_id}/issue")
def issue_ticket(booking_id: str, agency_id: str = Header(..., alias="X-Agency-ID")):
    booking = bookings.get(booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized booking access")
    if booking["status"] != "HELD":
        raise HTTPException(400, "Booking must be HELD")
    booking["status"] = "ISSUED"
    booking["ticket_id"] = "TKT-" + uuid4().hex[:8].upper()
    return {"message": "Ticket issued successfully", "booking": booking}


@app.patch("/bookings/{booking_id}/amend")
def amend_booking(booking_id: str, request: AmendmentRequest, agency_id: str = Header(..., alias="X-Agency-ID")):
    booking = bookings.get(booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized booking access")
    if booking["status"] != "ISSUED":
        raise HTTPException(400, "Only issued bookings can be amended")
    booking["passenger_name"] = request.passenger_name
    return {"message": "Booking amended successfully", "booking": booking}


@app.post("/bookings/{booking_id}/refund")
def refund_booking(booking_id: str, agency_id: str = Header(..., alias="X-Agency-ID")):
    booking = bookings.get(booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking["agency_id"] != agency_id:
        raise HTTPException(403, "Unauthorized booking access")
    if booking["status"] not in ("HELD", "ISSUED"):   # BUG 4: HELD should not be refundable
        raise HTTPException(400, "Booking cannot be refunded")
    time.sleep(0.2)                                   # BUG 5: slow payment call widens the race window
    booking["status"] = "REFUNDED"
    flights[booking["flight_id"]]["available_seats"] += booking["passengers"]
    return {"message": "Refund processed successfully", "refund_amount": booking["total_price"], "booking": booking}


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str, agency_id: str | None = Header(default=None, alias="X-Agency-ID")):
    booking = bookings.get(booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")
    # BUG 1: no ownership check
    return booking
