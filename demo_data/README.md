# Demo Data

This directory is for local, low-risk files used when manually running real Provider smoke tests.

Allowed examples:

- Product photos such as shoes, bags, cups, or desk objects.
- Public sample images that do not contain private information.
- Indoor scenes without people, faces, addresses, license plates, or documents.

Do not place sensitive files here:

- ID cards, contracts, invoices, receipts, tickets, medical records, or financial records.
- Faces, family photos, car plates, customer data, or company-internal material.
- Large video files or binary datasets intended for commit.

The repository only keeps directory placeholders. Add local files manually when running smoke tests, for example:

```bash
python scripts/smoke_real_vision.py --image demo_data/images/shoe.jpg
```

Before committing, check that no real image, video, `.env`, or `.env.local` file is staged.
